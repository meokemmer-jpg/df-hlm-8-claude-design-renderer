from __future__ import annotations

import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / 'src'))

from claude_design_renderer import ClaudeDesignRenderer, MutexGuard, WeeklyUC, ensure_html_lint


FIXED_NOW = datetime(2026, 5, 14, 6, 3, 2, tzinfo=timezone.utc)


def _copy_project_config(tmp_path: Path) -> Path:
    config = yaml.safe_load((PROJECT_ROOT / 'config.yaml').read_text(encoding='utf-8'))
    config['constraints']['k16']['lock_dir'] = str(tmp_path / 'df-hlm-8.lock')
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    return config_path


def _make_renderer(
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    chrome_uploader=None,
) -> ClaudeDesignRenderer:
    config_path = _copy_project_config(tmp_path)
    return ClaudeDesignRenderer(
        config_path,
        project_root=tmp_path,
        env=env or {},
        now_provider=lambda: FIXED_NOW,
        chrome_uploader=chrome_uploader,
    )


def _write_uc_input(directory: Path, file_name: str, payload: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / file_name
    if path.suffix == '.json':
        path.write_text(json.dumps(payload), encoding='utf-8')
    else:
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding='utf-8')


def _approved_uc(uc_id: str = 'UC-11', wave: str = 'wave-2', **extra: object) -> dict:
    return {
        'uc_id': uc_id,
        'wave': wave,
        'title': extra.get('title', 'Brand Voice Cloud'),
        'objective': extra.get('objective', 'Generate hospitality visuals'),
        'brand_voice': extra.get('brand_voice', 'Warm premium confidence'),
        'notes': extra.get('notes', 'Use latest Imke feedback'),
        'status': extra.get('status', 'approved'),
        'source_dfs': extra.get('source_dfs', ['df-hlm-6-approval-tracker', 'df-hlm-3']),
    }


def _seed_inputs(tmp_path: Path, *payloads: dict) -> None:
    input_dir = tmp_path / 'inputs' / 'imke-feedback-cohort'
    for index, payload in enumerate(payloads, start=1):
        _write_uc_input(input_dir, f'uc-{index}.yaml', payload)


def test_default_mock_mode_no_chrome_no_email(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    assert renderer.resolve_mode() == 'degraded_chrome_mcp'
    assert renderer.uses_mock_upload() is True


def test_env_var_true_real_mode(tmp_path: Path) -> None:
    renderer = _make_renderer(
        tmp_path,
        env={
            'DF_HLM_8_REAL_CHROME_MCP_ENABLED': 'true',
            'DF_HLM_8_REAL_EMAIL_SEND_ENABLED': 'true',
            'PHRONESIS_TICKET': 'PT-2026-XX-999',
        },
        chrome_uploader=lambda _brief: 'https://claude.ai/design/ART-001',
    )
    assert renderer.resolve_mode() == 'full'


def test_concurrent_spawn_protection(tmp_path: Path) -> None:
    lock_dir = tmp_path / 'mutex.lock'
    with MutexGuard(lock_dir):
        with pytest.raises(RuntimeError):
            with MutexGuard(lock_dir):
                pass


def test_cascade_containment(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    _seed_inputs(tmp_path, _approved_uc('UC-GOOD'), _approved_uc('UC-BAD'))
    original = renderer.generate_brief

    def wrapped(template: str, uc: WeeklyUC) -> str:
        if uc.uc_id == 'UC-BAD':
            raise ValueError('forced uc failure')
        return original(template, uc)

    renderer.generate_brief = wrapped  # type: ignore[method-assign]
    summary = renderer.run()
    assert any(item.uc_id == 'UC-GOOD' for item in summary.artifacts)
    assert any(item['uc_id'] == 'UC-BAD' for item in summary.failures)
    assert (tmp_path / 'output' / 'dlq' / 'wave-2' / 'UC-BAD.json').exists()


def test_external_anchor_artifact_url(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    _seed_inputs(tmp_path, _approved_uc())
    summary = renderer.run()
    assert summary.artifacts[0].cloud_url.startswith('https://claude.ai/design/MOCK-')


def test_circuit_breaker_open(tmp_path: Path) -> None:
    renderer = _make_renderer(
        tmp_path,
        env={
            'DF_HLM_8_REAL_CHROME_MCP_ENABLED': 'true',
            'PHRONESIS_TICKET': 'PT-2026-XX-100',
        },
        chrome_uploader=lambda _brief: (_ for _ in ()).throw(TimeoutError('Chrome down')),
    )
    for _ in range(3):
        with pytest.raises(TimeoutError):
            renderer._real_cloud_upload('brief')
        renderer.circuit_breaker.record_failure()
    assert renderer.circuit_breaker.is_open is True


def test_direct_mode_local_html_only(tmp_path: Path) -> None:
    renderer = _make_renderer(
        tmp_path,
        env={
            'DF_HLM_8_REAL_CHROME_MCP_ENABLED': 'true',
            'PHRONESIS_TICKET': 'PT-2026-XX-101',
        },
        chrome_uploader=lambda _brief: (_ for _ in ()).throw(TimeoutError('Chrome down')),
    )
    renderer.circuit_breaker.is_open = True
    _seed_inputs(tmp_path, _approved_uc())
    summary = renderer.run()
    assert summary.mode == 'standalone_local_html_only'
    assert summary.artifacts[0].cloud_url is None


def test_idempotent_uc_wave(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    _seed_inputs(tmp_path, _approved_uc())
    first = renderer.run()
    second = renderer.run()
    assert first.artifacts[0].artifact_id == second.artifacts[0].artifact_id
    state = json.loads((tmp_path / 'output' / 'state.json').read_text(encoding='utf-8'))
    assert len(state['artifacts']) == 1


def test_health_check_no_deps(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    assert renderer.health_check()['dependencies'] == []


def test_weekly_uc_detection_from_inputs(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    _write_uc_input(
        tmp_path / 'inputs' / 'imke-feedback-cohort',
        'batch.json',
        {'ucs': [_approved_uc('UC-11'), _approved_uc('UC-12', status='ready')]},
    )
    detected = renderer.detect_weekly_ucs()
    assert [uc.uc_id for uc in detected] == ['UC-11', 'UC-12']


def test_brief_generation_from_template(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    uc = WeeklyUC(
        uc_id='UC-11',
        wave='wave-2',
        title='Brand Voice Cloud',
        objective='Generate hospitality visuals',
        brand_voice='Warm premium confidence',
        notes='Use latest Imke feedback',
        source_dfs=['df-hlm-6-approval-tracker', 'df-hlm-4'],
    )
    brief = renderer.generate_brief(renderer.config['templates']['brief'], uc)
    assert 'UC-ID: UC-11' in brief
    assert 'Sources: df-hlm-6-approval-tracker, df-hlm-4' in brief


def test_html_lint_pre_upload(tmp_path: Path) -> None:
    _ = tmp_path
    ensure_html_lint('<html><head><title>X</title></head><body><p>ok</p></body></html>')
    with pytest.raises(ValueError):
        ensure_html_lint('not html')


def test_auto_render_limit_3_per_week(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path, env={'AUTO_RENDER_LIMIT_PER_WEEK': '3'})
    _seed_inputs(tmp_path, _approved_uc('UC-1'), _approved_uc('UC-2'), _approved_uc('UC-3'), _approved_uc('UC-4'))
    summary = renderer.run()
    assert len(summary.artifacts) == 3


def test_imke_email_draft_martin_approval_required(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    _seed_inputs(tmp_path, _approved_uc())
    summary = renderer.run()
    draft = Path(summary.email_draft_path).read_text(encoding='utf-8')
    assert 'MARTIN_APPROVAL_REQUIRED: true' in draft
    assert 'SEND_ALLOWED: false' in draft


def test_zip_backup_after_upload(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    _seed_inputs(tmp_path, _approved_uc())
    summary = renderer.run()
    zip_path = Path(summary.artifacts[0].zip_backup_path)
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == ['UC-11.html']


def test_provenance_in_output(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    _seed_inputs(tmp_path, _approved_uc())
    summary = renderer.run()
    artifact = summary.artifacts[0]
    assert artifact.provenance['artifact_id'] == artifact.artifact_id
    assert artifact.provenance['cloud_url'] == artifact.cloud_url
    assert artifact.provenance['source_dfs'] == artifact.source_dfs


def test_pre_action_domain_check(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    renderer.pre_action_domain_check('https://claude.ai/design/ART-1')
    with pytest.raises(ValueError):
        renderer.pre_action_domain_check('https://example.com/design/ART-1')


def test_audit_log_appended_per_run(tmp_path: Path) -> None:
    renderer = _make_renderer(tmp_path)
    _seed_inputs(tmp_path, _approved_uc())
    renderer.run()
    renderer.run()
    log_lines = (tmp_path / 'output' / 'audit.log').read_text(encoding='utf-8').strip().splitlines()
    assert len(log_lines) >= 4
