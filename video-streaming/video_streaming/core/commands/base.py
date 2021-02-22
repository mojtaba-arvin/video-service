"""
Base classes for writing commands
"""
from argparse import ArgumentParser

__all__ = [
    'BaseCommand'
]


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

    def add_arguments(self, parser):
        """
        Entry point for subclassed commands to add custom arguments.
        """
        pass
