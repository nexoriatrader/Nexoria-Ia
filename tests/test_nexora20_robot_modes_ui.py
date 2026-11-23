"""Contracts for the NEXORA 2.0 guided modes and planning-only manual schedule.

These tests intentionally do not start the app lifespan or place Deriv orders.
"""
import ast
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app/main.py").read_text(encoding="utf-8")
DB = (ROOT / "app/db.py").read_text(encoding="utf-8")
MIGRATION = (ROOT / "migrations/20261123_robot_modes_manual_signals_homologation.sql").read_text(
    encoding="utf-8"
)


class Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if "id" in d:
            self.ids.append(d["id"])


def test_syntax_and_unique_dom_ids():
    ast.parse(MAIN)
    ast.parse(DB)
    parser = Ids()
    parser.feed(HTML)
    assert len(parser.ids) == len(set(parser.ids))


def test_five_steps_with_two_clear_modes_and_conditional_manual_button():
    wizard = HTML.split('id="nxWizard2"', 1)[1].split('id="automationConfigCard"', 1)[0]
    assert wizard.count('data-nx-simple="') == 5
    assert 'data-nx-mode="intelligent"' in wizard
    assert 'data-nx-mode="manual"' in wizard
    assert 'id="nxIntelligentCycles"' in wizard
    assert 'id="nxManualRecovery"' in wizard
    assert 'id="nxCyclesFields"' in wizard
    assert 'id="nxMartingaleFields"' in wizard
    assert 'id="nxWin"' in wizard and 'id="nxLoss"' in wizard
    assert 'id="nxReview"' in wizard
    assert 'onclick="nxSaveConfiguration()"' in wizard
    assert 'onclick="toggleRobot()"' not in wizard
    dashboard = HTML.split('id="configureRobotButton"', 1)[1].split('id="robotStartNotice"', 1)[0]
    assert 'id="nxManualScheduleButton"' in dashboard
    assert 'hidden onclick="go(\'robot-manual-signals\')"' in dashboard
    assert "btn.hidden=r.mode!=='manual'" in HTML


def test_manual_agenda_has_only_planning_actions_and_uses_real_catalog():
    page = HTML.split('<section id="robot-manual-signals"', 1)[1].split('<section id="results"', 1)[0]
    for field in ('nxSignalAsset', 'nxSignalDate', 'nxSignalSide',
                  'nxSignalDuration', 'nxSignalList'):
        assert f'id="{field}"' in page
    assert 'NÃO executa operações' in page
    block = MAIN.split('class RobotModeSelection(BaseModel):', 1)[1].split(
        "@app.get('/api/automation/catalogue')", 1
    )[0]
    for path in ("/api/robot/mode", "/api/robot/manual-signals"):
        assert path in block
    assert "featured_instruments_by_group(market)" in block
    assert "robot_mode_for(session, user.id) != 'manual'" in block
    assert "'SCHEDULE_ONLY_NO_ORDERS'" in block
    for forbidden in ('demo_buy(', 'real_buy(', '/api/autopilot/start', 'START_DEMO_AUTOPILOT'):
        assert forbidden not in block


def test_manual_blocks_automatic_start_without_weakened_gates():
    start = MAIN.split("async def autopilot_start(", 1)[1].split(
        "@app.post('/api/autopilot/stop')", 1
    )[0]
    assert "robot_mode_for(session, user.id) == 'manual'" in start
    assert 'execução automática de sinais ainda não está habilitada' in start
    assert "if not autopilot_allowed()" in start
    assert "if not v2_enabled()" in start
    assert 'autopilot_lock' in start
    assert 'real_runtime' in MAIN
    assert "if user.selected_mode!='demo'" in start


def test_schema_requires_formal_migration_and_never_auto_ddls_postgresql():
    assert 'class RobotUsageMode(Base):' in DB
    assert 'class RobotManualSignal(Base):' in DB
    assert 'verify_postgresql_schema_read_only(connection)' in DB
    assert 'if engine.dialect.name == "postgresql":' in DB
    for name in ('robot_usage_modes', 'robot_manual_signals'):
        assert name in MIGRATION
        assert name in DB
    assert 'NÃO executar em produção' in MIGRATION
    assert 'ON DELETE CASCADE' in MIGRATION
