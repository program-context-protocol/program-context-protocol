"""PCP CLI entry point."""

import click
from pcp.commands.validate_strategy import validate_strategy
from pcp.commands.init import init
from pcp.commands.scan import scan
from pcp.commands.diff import diff
from pcp.commands.check import check
from pcp.commands.deploy_check import deploy_check
from pcp.commands.gate import gate


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
