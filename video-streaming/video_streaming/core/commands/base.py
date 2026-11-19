"""
Base classes for writing commands
"""
from argparse import ArgumentParser


class BaseCommand:
    """
    The base class
    """
    # Metadata about this command.
    help = ''

    def create_parser(self):
        """
        Create the ``ArgumentParser`` to parse the arguments.
        """
        parser = ArgumentParser(description=self.help)
        self.add_arguments(parser)
        return parser

    def parse_args(self):
        parser = self.create_parser()
        args = parser.parse_args()
        return args

    def add_arguments(self, parser):
        """
        Entry point for subclassed commands to add custom arguments.
        """
        pass
