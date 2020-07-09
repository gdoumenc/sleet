import sys
from abc import ABC, abstractmethod

from coworks import TechMicroService


class CwsCommandOptions:

    def __init__(self, cmd, **kwargs):
        self.__cmd = cmd
        self.__options = kwargs

        assert 'project_dir' in kwargs
        assert 'module' in kwargs
        assert 'workspace' in kwargs

        if 'service' not in kwargs:
            self.__options['service'] = cmd.app

    @property
    def project_dir(self):
        return self.__options['project_dir']

    @property
    def module(self):
        return self.__options['module']

    @property
    def service(self):
        return self.__options['service']

    @property
    def workspace(self):
        return self.__options['workspace']

    def __getitem__(self, key):
        return self.__options.get(key)

    def __setitem__(self, key, value):
        self.__options[key] = value

    def __contains__(self, key):
        return key in self.__options

    def keys(self):
        return self.__options.keys()

    def to_dict(self):
        return self.__options

    def __repr__(self):
        return str(self.__options)

    def setdefault(self, key, value):
        if self.__options.get(key) is None:
            self.__options[key] = value

class CwsCommand(ABC):

    def __init__(self, app: TechMicroService = None, *, name):
        self.name = name

        # Trace interfaces.
        self.output = sys.stdout
        self.error = sys.stderr

        # A list of functions that will be called before or after the command executioon.
        self.before_funcs = []
        self.after_funcs = []

        if app is not None:
            self.app = app
            self.init_app(app)

    def init_app(self, app):
        app.commands[self.name] = self

    @property
    def options(self):
        return ()

    def execute(self, *, options:CwsCommandOptions, output=None, error=None):
        """ Called when the command is called.
        :param output: output stream.
        :param error: error stream.
        :param options: command options.
        :return: None
        """
        self.app.deferred_init(options.workspace)

        if output is not None:
            self.output = open(output, 'w+') if type(output) is str else output
        if error is not None:
            self.error = open(error, 'w+') if type(error) is str else error

        for func in self.before_funcs:
            func(options)

        self._execute(options)

        for func in self.after_funcs:
            func(options)

    def before_execute(self, f):
        """Registers a function to be run before the command execution.
        :param f: function called before the command execution
        :return: None

        May be used as a decorator.

        The function will be called without any arguments and its return value is ignored.
        """

        self.before_funcs.append(f)
        return f

    def after_execute(self, f):
        """Registers a function to be run after the command execution.
        :param f: function called after the command execution
        :return: None

        May be used as a decorator.

        The function will be called without any arguments and its return value is ignored.
        """

        self.after_funcs.append(f)
        return f

    @abstractmethod
    def _execute(self, options):
        """ Main command function.
        :param options: Command options.
        :return: None.

        Abstract method which must be redefined in any subclass. The content should be written in self.output.
        """
