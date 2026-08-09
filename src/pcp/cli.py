"""PCP CLI entry point."""

import click
from pcp.commands.validate_strategy import validate_strategy
from pcp.commands.init import init
from pcp.commands.scan import scan
from pcp.commands.diff import diff
from pcp.commands.check import check
from pcp.commands.deploy_check import deploy_check
from pcp.commands.gate import gate
from pcp.commands.context import context
from pcp.commands.validate_module import validate_module
from pcp.commands.report import report
from pcp.commands.install_hook import install_hook
from pcp.commands.install_skill import install_skill
from pcp.commands.status import status
from pcp.commands.architect_review import architect_review
from pcp.commands.kickoff import kickoff
from pcp.commands.pm import pm
from pcp.commands.build import build
from pcp.commands.import_project import import_project
from pcp.commands.self_update import self_update
from pcp.commands.audit import audit
from pcp.commands.telemetry_cmd import telemetry_cmd
from pcp.commands.doctor import doctor
from pcp.commands.watch import watch
from pcp.commands.deploy import deploy
from pcp.commands.capture import capture
from pcp.commands.provenance import provenance
from pcp.commands.takeover import takeover
from pcp.commands.dashboard import dashboard
from pcp.commands.verify_syntax_fix import verify_syntax_fix
from pcp.commands.architecture_justification import architecture_justification
from pcp.commands.design_audit import design_audit
from pcp.commands.docs import docs
from pcp.commands.prune import prune
from pcp.commands.escalations_cmd import escalations_cmd
from pcp.commands.pressure_test_cmd import pressure_test_cmd
from pcp.commands.control_audit_cmd import control_audit_cmd
from pcp.commands.objective_conflicts_cmd import objective_conflicts_cmd
from pcp.commands.correct_objective import correct_objective
from pcp.commands.amend import amend
from pcp.commands.inspiration_art import inspiration_art
from pcp.commands.enrich import enrich
from pcp.commands.run_log_cmd import run_log_cli
from pcp.commands.narrative_lint import narrative_lint_cmd
from pcp.commands.build_status import build_status
from pcp.commands.verify import verify
from pcp.commands.build_plan import build_plan_cmd
from pcp.commands.diff_reduce import diff_reduce
from pcp.commands.traceability import traceability
from pcp.commands.assumptions_cmd import assumptions_cmd


@click.group()
@click.version_option(package_name="program-context-protocol")
def cli():
    """PCP — Program Context Protocol.

    Prevent LLM hallucination and context drift across dev sessions.
    """


cli.add_command(init)
cli.add_command(scan)
cli.add_command(diff)
cli.add_command(check)
cli.add_command(gate)
cli.add_command(deploy_check, name="deploy-check")
cli.add_command(validate_strategy, name="validate-strategy")
cli.add_command(validate_module, name="validate-module")
cli.add_command(context)
cli.add_command(report)
cli.add_command(install_hook, name="install-hook")
cli.add_command(install_skill)
cli.add_command(status)
cli.add_command(architect_review, name="architect-review")
cli.add_command(kickoff)
cli.add_command(pm)
cli.add_command(build)
cli.add_command(import_project, name="import")
cli.add_command(audit)
cli.add_command(telemetry_cmd)
cli.add_command(doctor)
cli.add_command(watch)
cli.add_command(deploy)
cli.add_command(capture)
cli.add_command(provenance)
cli.add_command(takeover)
cli.add_command(dashboard)
cli.add_command(verify_syntax_fix)
cli.add_command(architecture_justification)
cli.add_command(design_audit)
cli.add_command(docs)
cli.add_command(prune)
cli.add_command(escalations_cmd)
cli.add_command(pressure_test_cmd)
cli.add_command(control_audit_cmd)
cli.add_command(objective_conflicts_cmd)
cli.add_command(correct_objective)
cli.add_command(amend)
cli.add_command(inspiration_art)
cli.add_command(enrich)
cli.add_command(run_log_cli, name="run-log")
cli.add_command(narrative_lint_cmd)
cli.add_command(build_status)
cli.add_command(verify)
cli.add_command(build_plan_cmd, name="build-plan")
cli.add_command(self_update)
cli.add_command(diff_reduce)
cli.add_command(traceability)
cli.add_command(assumptions_cmd, name="assumptions")
