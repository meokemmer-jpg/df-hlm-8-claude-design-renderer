from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _df_common.pii_scrubber import PIIScrubber, scrub_audit_payload
from _df_common.welle_b2_patches import (
    K13PreActionVerifier,
    K16MutexGuard,
    MOCK_PREFIX,
    make_mock_url,
    make_provenance_envelope,
)

try:
    import structlog
except ModuleNotFoundError:  # pragma: no cover - dependency fallback
    class _JsonRenderer:
        def __call__(self, _logger: object, event: str, event_dict: dict[str, Any]) -> str:
            payload = {'event': event} | event_dict
            return json.dumps(payload, sort_keys=True)

    class _WriteLogger:
        def __init__(self, file: Any) -> None:
            self.file = file

        def info(self, message: str) -> None:
            self.file.write(message + '\n')

    class _WriteLoggerFactory:
        def __init__(self, file: Any) -> None:
            self.file = file

        def __call__(self) -> _WriteLogger:
            return _WriteLogger(self.file)

    class _StructlogFallback:
        class processors:
            @staticmethod
            def JSONRenderer() -> _JsonRenderer:
                return _JsonRenderer()

        @staticmethod
        def WriteLoggerFactory(file: Any) -> _WriteLoggerFactory:
            return _WriteLoggerFactory(file)

        @staticmethod
        def wrap_logger(logger: _WriteLogger, processors: list[Any]) -> Any:
            renderer = processors[-1]

            class _BoundLogger:
                def info(self, event: str, **fields: Any) -> None:
                    logger.info(renderer(None, event, fields))

            return _BoundLogger()

    structlog = _StructlogFallback()

import yaml
from lxml import etree, html


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def ensure_html_lint(html_text: str) -> None:
    if '<html' not in html_text.lower() or '<body' not in html_text.lower():
        raise ValueError('HTML lint failed: missing html/body root')
    parser = etree.HTMLParser(recover=False)
    document = html.document_fromstring(html_text, parser=parser)
    if not document.xpath('//title'):
        raise ValueError('HTML lint failed: missing <title>')
    if parser.error_log:
        raise ValueError(f'HTML lint failed: {parser.error_log[0]}')


@dataclass(slots=True)
class WeeklyUC:
    uc_id: str
    wave: str
    title: str
    objective: str
    brand_voice: str
    notes: str
    source_dfs: list[str]
    status: str = 'approved'


@dataclass(slots=True)
class RenderArtifact:
    uc_id: str
    wave: str
    mode: str
    local_html_path: str
    zip_backup_path: str
    cloud_url: str | None
    artifact_id: str | None
    timestamp: str
    source_dfs: list[str]
    provenance: dict[str, Any]


@dataclass(slots=True)
class RenderSummary:
    wave: str
    mode: str
    artifacts: list[RenderArtifact] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    email_draft_path: str | None = None
    report_path: str | None = None
    cloud_url_list_path: str | None = None


class CircuitBreaker:
    def __init__(self, timeout_s: int, open_threshold: int) -> None:
        self.timeout_s = timeout_s
        self.open_threshold = open_threshold
        self.failure_count = 0
        self.is_open = False

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.open_threshold:
            self.is_open = True

    def reset(self) -> None:
        self.failure_count = 0
        self.is_open = False


class MutexGuard:
    def __init__(self, lock_dir: Path) -> None:
        self.lock_dir = lock_dir
        self.acquired = False

    def acquire(self) -> None:
        try:
            self.lock_dir.mkdir(parents=False, exist_ok=False)
            self.acquired = True
        except FileExistsError as exc:
            raise RuntimeError(f'K16 mutex active: {self.lock_dir}') from exc

    def release(self) -> None:
        if self.acquired and self.lock_dir.exists():
            self.lock_dir.rmdir()
            self.acquired = False

    def __enter__(self) -> 'MutexGuard':
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class ClaudeDesignRenderer:
    def __init__(
        self,
        config_path: Path | str,
        *,
        project_root: Path | None = None,
        env: Mapping[str, str] | None = None,
        now_provider: Callable[[], datetime] = utc_now,
        chrome_uploader: Callable[[str], str] | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.project_root = project_root or self.config_path.parent
        self.env = dict(os.environ if env is None else env)
        self.now_provider = now_provider
        self.chrome_uploader = chrome_uploader
        self.config = self._load_config()
        self.paths = self._resolve_paths()
        self.output_dir: Path = self.paths['output_dir']
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_path: Path = self.paths['state_json']
        self.input_dirs: list[Path] = self.paths['input_dirs']
        self.pii_scrubber = PIIScrubber(enabled=True, kemmer_names_enabled=True)
        self.circuit_breaker = CircuitBreaker(
            timeout_s=self.config['lose_coupling']['lc3']['timeout_s'],
            open_threshold=self.config['lose_coupling']['lc3']['open_threshold'],
        )
        self._audit_handle = None
        self.logger = self._build_logger()

    def _load_config(self) -> dict[str, Any]:
        with self.config_path.open('r', encoding='utf-8') as handle:
            return yaml.safe_load(handle)

    def _resolve_paths(self) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, raw_value in self.config['paths'].items():
            if isinstance(raw_value, list):
                resolved[key] = [(self.project_root / value).resolve() for value in raw_value]
            else:
                resolved[key] = (self.project_root / raw_value).resolve()
        return resolved

    def _build_logger(self) -> Any:
        audit_path: Path = self.paths['audit_log']
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._audit_handle = audit_path.open('a', encoding='utf-8')
        factory = structlog.WriteLoggerFactory(file=self._audit_handle)
        return structlog.wrap_logger(factory(), processors=[structlog.processors.JSONRenderer()])

    def health_check(self) -> dict[str, Any]:
        return {
            'healthy': True,
            'dependencies': self.config['lose_coupling']['lc5']['health_check_dependencies'],
            'circuit_breaker_open': self.circuit_breaker.is_open,
        }

    def env_gate(self, key: str) -> bool:
        env_var = self.config['env_gating'][key]['env_var']
        default = self.config['env_gating'][key].get('default', 'false')
        return self.env.get(env_var, default).lower() == 'true'

    def phronesis_ticket(self) -> str | None:
        return self.env.get(self.config['env_gating']['phronesis_ticket']['env_var'])

    def auto_render_limit(self) -> int:
        env_name = self.config['env_gating']['auto_render_limit_per_week']['env_var']
        default = self.config['env_gating']['auto_render_limit_per_week']['default']
        return int(self.env.get(env_name, default))

    def resolve_mode(self) -> str:
        if self.circuit_breaker.is_open:
            return 'standalone_local_html_only'
        chrome_enabled = self.env_gate('df_hlm_8_real_chrome_mcp_enabled')
        email_enabled = self.env_gate('df_hlm_8_real_email_send_enabled')
        if chrome_enabled and email_enabled:
            return 'full'
        if chrome_enabled:
            return 'degraded_email_send'
        return 'degraded_chrome_mcp'

    def uses_mock_upload(self) -> bool:
        return not self.env_gate('df_hlm_8_real_chrome_mcp_enabled')

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {'artifacts': {}, 'runs': 0}
        return json.loads(self.state_path.read_text(encoding='utf-8'))

    def _write_text_output(self, path: Path, content: str) -> None:
        atomic_write_text(path, self.pii_scrubber.scrub(content))

    def _write_json_output(self, path: Path, payload: Any) -> None:
        scrubbed = self._scrub_payload(payload)
        atomic_write_json(path, scrubbed)

    def _scrub_payload(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            return self.pii_scrubber.scrub_dict_recursive(payload)
        if isinstance(payload, list):
            return [self._scrub_payload(item) for item in payload]
        if isinstance(payload, str):
            return self.pii_scrubber.scrub(payload)
        return payload

    def _save_state(self, state: dict[str, Any]) -> None:
        self._write_json_output(self.state_path, state)

    def pre_action_domain_check(self, target_url: str) -> None:
        if not self.config['constraints']['k13']['pre_action_domain_check']:
            return
        parsed = urlparse(target_url)
        if parsed.scheme != 'https' or parsed.netloc != 'claude.ai' or not parsed.path.startswith('/design'):
            raise ValueError(f'K13 domain check failed: {target_url}')

    def detect_weekly_ucs(self, input_dirs: list[Path] | None = None) -> list[WeeklyUC]:
        allowed_sources = set(self.config['execution']['weekly_uc_sources'])
        candidates: list[WeeklyUC] = []
        seen: set[tuple[str, str]] = set()
        for directory in input_dirs or self.input_dirs:
            if not directory.exists():
                continue
            for path in sorted(directory.iterdir()):
                if path.suffix.lower() not in {'.json', '.yaml', '.yml'}:
                    continue
                payload = self._read_payload(path)
                items = payload if isinstance(payload, list) else payload.get('ucs', [payload])
                for item in items:
                    source_dfs = item.get('source_dfs', [])
                    if not source_dfs or not set(source_dfs).intersection(allowed_sources):
                        continue
                    status = str(item.get('status', '')).lower()
                    if status not in {'approved', 'ready', 'new'} and not item.get('render_required', False):
                        continue
                    uc = WeeklyUC(
                        uc_id=item['uc_id'],
                        wave=item.get('wave', self.config['execution']['weekly_wave']),
                        title=item.get('title', item['uc_id']),
                        objective=item.get('objective', 'Generate HeyLou marketing render'),
                        brand_voice=item.get('brand_voice', 'HeyLou confident hospitality'),
                        notes=item.get('notes', ''),
                        source_dfs=list(source_dfs),
                        status=status or 'approved',
                    )
                    key = (uc.wave, uc.uc_id)
                    if key not in seen:
                        candidates.append(uc)
                        seen.add(key)
        return candidates

    def _read_payload(self, path: Path) -> dict[str, Any] | list[dict[str, Any]]:
        raw = path.read_text(encoding='utf-8')
        if path.suffix.lower() == '.json':
            return json.loads(raw)
        return yaml.safe_load(raw)

    def generate_brief(self, template: str, uc: WeeklyUC) -> str:
        values = asdict(uc) | {'timestamp': self.now_provider().isoformat()}
        values['source_dfs'] = ', '.join(uc.source_dfs)
        required = {field_name for _, field_name, _, _ in Formatter().parse(template) if field_name}
        missing = sorted(field for field in required if field not in values)
        if missing:
            raise ValueError(f'Brief template missing values: {missing}')
        return template.format(**values)

    def build_local_html(self, uc: WeeklyUC, brief_text: str) -> str:
        return (
            '<!DOCTYPE html>\n'
            '<html lang="en">\n'
            '<head><meta charset="utf-8"><title>'
            f'{uc.uc_id} - {uc.title}'
            '</title></head>\n'
            '<body>\n'
            f'<h1>{uc.title}</h1>\n'
            f'<p><strong>Wave:</strong> {uc.wave}</p>\n'
            f'<p><strong>Objective:</strong> {uc.objective}</p>\n'
            f'<p><strong>Brand Voice:</strong> {uc.brand_voice}</p>\n'
            f'<pre>{brief_text}</pre>\n'
            '</body>\n'
            '</html>\n'
        )

    def _backup_html(self, uc: WeeklyUC, html_text: str) -> tuple[Path, Path]:
        render_root: Path = self.paths['branch_hub_render_root'] / uc.wave
        html_path = render_root / f'{uc.uc_id}.html'
        zip_path = render_root / f'{uc.uc_id}.zip'
        html_text_scrubbed = self.pii_scrubber.scrub(html_text)
        atomic_write_text(html_path, html_text_scrubbed)
        render_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f'{uc.uc_id}.html', html_text_scrubbed)
        return html_path, zip_path

    def _mock_cloud_upload(self, uc: WeeklyUC) -> tuple[str, str]:
        token = f'{uc.wave}-{uc.uc_id}'
        url = make_mock_url('https://claude.ai/design', token)
        return url, url.rstrip('/').split('/')[-1]

    def _verify_real_mode_pre_action(self) -> None:
        verifier = K13PreActionVerifier(
            expected_env_tag='dev',
            expected_mount_pattern='/Users/make',
            blast_radius_class='state-only',
        )
        result = verifier.verify()
        if not result.ok:
            raise RuntimeError(f'K13-VETO: {result.failed_check}')

    def _real_cloud_upload(self, brief_text: str) -> tuple[str, str]:
        self._verify_real_mode_pre_action()
        self.pre_action_domain_check('https://claude.ai/design')
        ticket = self.phronesis_ticket()
        if not ticket or not ticket.startswith('PT-'):
            raise ValueError('Real Chrome-MCP mode requires PHRONESIS_TICKET')
        if self.chrome_uploader is None:
            raise TimeoutError('Chrome-MCP unreachable >30s')
        url = self.chrome_uploader(brief_text)
        self.pre_action_domain_check(url)
        artifact_id = url.rstrip('/').split('/')[-1]
        return url, artifact_id

    def _write_cloud_url_list(self, artifacts: list[RenderArtifact]) -> Path:
        payload = [
            {
                'uc_id': artifact.uc_id,
                'wave': artifact.wave,
                'cloud_url': artifact.cloud_url,
                'artifact_id': artifact.artifact_id,
                'timestamp': artifact.timestamp,
                'source_dfs': artifact.source_dfs,
                'provenance': artifact.provenance,
            }
            for artifact in artifacts
        ]
        path: Path = self.paths['cloud_url_list_json']
        self._write_json_output(path, payload)
        return path

    def _write_report(self, summary: RenderSummary) -> Path:
        lines = [
            f'# Weekly Render Report - {summary.wave}',
            '',
            f'Mode: {summary.mode}',
            '',
            '## Artifacts',
        ]
        for artifact in summary.artifacts:
            lines.extend(
                [
                    f'- {artifact.uc_id}: {artifact.cloud_url or "local-html-only"}',
                    f'  - artifact_id: {artifact.artifact_id or "n/a"}',
                    f'  - timestamp: {artifact.timestamp}',
                    f'  - provenance: {json.dumps(artifact.provenance, sort_keys=True)}',
                ]
            )
        if summary.failures:
            lines.extend(['', '## Failures'])
            for failure in summary.failures:
                lines.append(f"- {failure['uc_id']}: {failure['error']}")
        path: Path = self.paths['report_markdown']
        self._write_text_output(path, '\n'.join(lines) + '\n')
        return path

    def _write_email_draft(self, summary: RenderSummary) -> Path:
        body = self.config['templates']['imke_email_body'].format(
            wave=summary.wave,
            cloud_urls='\n'.join(artifact.cloud_url or artifact.local_html_path for artifact in summary.artifacts),
            provenance='\n'.join(json.dumps(artifact.provenance, sort_keys=True) for artifact in summary.artifacts),
        )
        subject = self.config['templates']['imke_email_subject'].format(wave=summary.wave)
        draft_path: Path = self.paths['email_drafts_dir'] / f'{summary.wave}-imke-draft.txt'
        content = (
            'TO: imke@heylouhotels.com\n'
            f'SUBJECT: {subject}\n'
            'MARTIN_APPROVAL_REQUIRED: true\n'
            'SEND_ALLOWED: false\n\n'
            f'{body}\n'
        )
        self._write_text_output(draft_path, content)
        return draft_path

    def _append_audit(self, event: str, **fields: Any) -> None:
        entry_scrubbed = scrub_audit_payload({'event': event, **fields})
        event_scrubbed = str(entry_scrubbed.pop('event'))
        self.logger.info(event_scrubbed, **entry_scrubbed)
        if self._audit_handle is not None:
            self._audit_handle.flush()

    def _dlq_write(self, uc: WeeklyUC, error: Exception) -> None:
        dlq_path: Path = self.paths['dlq_dir'] / uc.wave / f'{uc.uc_id}.json'
        self._write_json_output(
            dlq_path,
            {
                'uc_id': uc.uc_id,
                'wave': uc.wave,
                'error': str(error),
                'timestamp': self.now_provider().isoformat(),
            },
        )

    def run(self) -> RenderSummary:
        stop_flag: Path = self.paths['stop_flag']
        if stop_flag.exists():
            raise RuntimeError('K14 STOP.flag active')
        state = self._load_state()
        summary = RenderSummary(wave=self.config['execution']['weekly_wave'], mode=self.resolve_mode())
        if self.env_gate('df_hlm_8_real_chrome_mcp_enabled'):
            self._verify_real_mode_pre_action()
        weekly_ucs = self.detect_weekly_ucs()
        limited_ucs = weekly_ucs[: self.auto_render_limit()]
        with K16MutexGuard(lock_dir='/tmp/df-hlm-8.lock', df_engine_marker='claude_design_renderer.py'):
            self._append_audit('run_started', mode=summary.mode, candidate_count=len(weekly_ucs))
            for uc in limited_ucs:
                key = f'{uc.wave}:{uc.uc_id}'
                if key in state['artifacts']:
                    summary.artifacts.append(RenderArtifact(**state['artifacts'][key]))
                    continue
                try:
                    brief = self.generate_brief(self.config['templates']['brief'], uc)
                    html_text = self.build_local_html(uc, brief)
                    ensure_html_lint(html_text)
                    local_html_path, zip_backup_path = self._backup_html(uc, html_text)
                    if summary.mode == 'standalone_local_html_only':
                        cloud_url, artifact_id = None, None
                    elif self.env_gate('df_hlm_8_real_chrome_mcp_enabled'):
                        try:
                            cloud_url, artifact_id = self._real_cloud_upload(brief)
                            self.circuit_breaker.reset()
                        except TimeoutError:
                            self.circuit_breaker.record_failure()
                            summary.mode = 'standalone_local_html_only'
                            cloud_url, artifact_id = None, None
                        except Exception:
                            self.circuit_breaker.record_failure()
                            raise
                    else:
                        cloud_url, artifact_id = self._mock_cloud_upload(uc)
                    timestamp = self.now_provider().isoformat()
                    is_mock = artifact_id is not None and artifact_id.startswith(MOCK_PREFIX)
                    provenance = make_provenance_envelope(
                        df_id='DF-HLM-8',
                        timestamp_iso=timestamp,
                        is_mock=is_mock,
                        activation_gate_id=None if is_mock else self.phronesis_ticket(),
                    ) | {
                        'cloud_url': cloud_url,
                        'artifact_id': artifact_id,
                        'timestamp': timestamp,
                        'source_dfs': uc.source_dfs,
                    }
                    artifact = RenderArtifact(
                        uc_id=uc.uc_id,
                        wave=uc.wave,
                        mode=summary.mode,
                        local_html_path=str(local_html_path),
                        zip_backup_path=str(zip_backup_path),
                        cloud_url=cloud_url,
                        artifact_id=artifact_id,
                        timestamp=timestamp,
                        source_dfs=uc.source_dfs,
                        provenance=provenance,
                    )
                    state['artifacts'][key] = asdict(artifact)
                    summary.artifacts.append(artifact)
                    self._append_audit('uc_rendered', uc_id=uc.uc_id, wave=uc.wave, cloud_url=cloud_url)
                except Exception as exc:
                    self._dlq_write(uc, exc)
                    summary.failures.append({'uc_id': uc.uc_id, 'error': str(exc)})
                    self._append_audit('uc_failed', uc_id=uc.uc_id, error=str(exc))
            state['runs'] = state.get('runs', 0) + 1
            self._save_state(state)
            run_complete_event = 'mock_run_complete' if any(
                artifact.artifact_id and artifact.artifact_id.startswith(MOCK_PREFIX)
                for artifact in summary.artifacts
            ) else 'run_complete'
        summary.cloud_url_list_path = str(self._write_cloud_url_list(summary.artifacts))
        summary.report_path = str(self._write_report(summary))
        summary.email_draft_path = str(self._write_email_draft(summary))
        self._append_audit(
            run_complete_event,
            mode=summary.mode,
            rendered=len(summary.artifacts),
            failed=len(summary.failures),
            email_draft_path=summary.email_draft_path,
        )
        return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='DF-HLM-8 Claude Design Auto Renderer')
    parser.add_argument('--config', default='config.yaml')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    renderer = ClaudeDesignRenderer(args.config)
    summary = renderer.run()
    print(json.dumps(asdict(summary), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
