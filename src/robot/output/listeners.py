#  Copyright 2008-2015 Nokia Solutions and Networks
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import inspect
import os.path

from robot.errors import DataError
from robot.utils import (get_error_details, is_string, is_list_like,
                         is_dict_like, py2to3, split_args_from_name_or_path,
                         type_name, Importer)

from .loggerhelper import AbstractLoggerProxy
from .logger import LOGGER


def no_recursion(cls):
    """Class decorator to wrap methods so that they cannot cause recursion.

    Recursion would otherwise happen if one listener logs something and that
    message is received and logged again by log_message or message method.
    """
    def avoid_recursion_wrapper(method):
        def avoid_recursion(self, *args):
            if not self._recursion:
                self._recursion = True
                method(self, *args)
                self._recursion = False
        return avoid_recursion
    for attr, value in cls.__dict__.items():
        if not attr.startswith('_') and inspect.isroutine(value):
            setattr(cls, attr, avoid_recursion_wrapper(value))
    cls._recursion = False
    return cls


@no_recursion
@py2to3
class Listeners(object):
    _start_attrs = ('id', 'doc', 'starttime', 'longname')
    _end_attrs = _start_attrs + ('endtime', 'elapsedtime', 'status', 'message')
    _kw_extra_attrs = ('args', 'assign', 'kwname', 'libname',
                       '-id', '-longname', '-message')

    def __init__(self, listeners):
        if listeners is not None:
            self._listeners = list(self._import_listeners(listeners))
        self._running_test = False
        self._setup_or_teardown_type = None

    def __nonzero__(self):
        return bool(self._listeners)

    def _import_listeners(self, listeners):
        for listener in listeners:
            try:
                yield ListenerProxy(listener)
            except DataError as err:
                if not is_string(listener):
                    listener = type_name(listener)
                LOGGER.error("Taking listener '%s' into use failed: %s"
                             % (listener, err.message))

    def start_suite(self, suite):
        for listener in self._listeners:
            attrs = self._get_start_attrs(suite, 'metadata')
            attrs.update(self._get_suite_attrs(suite))
            listener.call_method(listener.start_suite, suite.name, attrs)

    def _get_suite_attrs(self, suite):
        return {
            'tests' : [t.name for t in suite.tests],
            'suites': [s.name for s in suite.suites],
            'totaltests': suite.test_count,
            'source': suite.source or ''
        }

    def end_suite(self, suite):
        for listener in self._listeners:
            self._notify_end_suite(listener, suite)

    def _notify_end_suite(self, listener, suite):
        attrs = self._get_end_attrs(suite, 'metadata')
        attrs['statistics'] = suite.stat_message
        attrs.update(self._get_suite_attrs(suite))
        listener.call_method(listener.end_suite, suite.name, attrs)

    def start_test(self, test):
        self._running_test = True
        for listener in self._listeners:
            attrs = self._get_start_attrs(test, 'tags')
            attrs['critical'] = 'yes' if test.critical else 'no'
            attrs['template'] = test.template or ''
            listener.call_method(listener.start_test, test.name, attrs)

    def end_test(self, test):
        self._running_test = False
        for listener in self._listeners:
            self._notify_end_test(listener, test)

    def _notify_end_test(self, listener, test):
        attrs = self._get_end_attrs(test, 'tags')
        attrs['critical'] = 'yes' if test.critical else 'no'
        attrs['template'] = test.template or ''
        listener.call_method(listener.end_test, test.name, attrs)

    def start_keyword(self, kw):
        for listener in self._listeners:
            attrs = self._get_start_attrs(kw, *self._kw_extra_attrs)
            attrs['type'] = self._get_keyword_type(kw, start=True)
            listener.call_method(listener.start_keyword, kw.name, attrs)

    def end_keyword(self, kw):
        for listener in self._listeners:
            attrs = self._get_end_attrs(kw, *self._kw_extra_attrs)
            attrs['type'] = self._get_keyword_type(kw, start=False)
            listener.call_method(listener.end_keyword, kw.name, attrs)

    def _get_keyword_type(self, kw, start=True):
        # When running setup or teardown, only the top level keyword has type
        # set to setup/teardown but we want to pass that type also to all
        # start/end_keyword listener methods called below that keyword.
        if kw.type == 'kw':
            return self._setup_or_teardown_type or 'Keyword'
        kw_type = self._get_setup_or_teardown_type(kw)
        self._setup_or_teardown_type = kw_type if start else None
        return kw_type

    def _get_setup_or_teardown_type(self, kw):
        return '%s %s' % (('Test' if self._running_test else 'Suite'),
                          kw.type.title())

    def imported(self, import_type, name, attrs):
        for listener in self._listeners:
            method = getattr(listener, '%s_import' % import_type.lower())
            listener.call_method(method, name, attrs)

    def log_message(self, msg):
        for listener in self._listeners:
            listener.call_method(listener.log_message, self._create_msg_dict(msg))

    def message(self, msg):
        for listener in self._listeners:
            listener.call_method(listener.message, self._create_msg_dict(msg))

    def _create_msg_dict(self, msg):
        return {'timestamp': msg.timestamp, 'message': msg.message,
                'level': msg.level, 'html': 'yes' if msg.html else 'no'}

    def output_file(self, file_type, path):
        for listener in self._listeners:
            method = getattr(listener, '%s_file' % file_type.lower())
            listener.call_method(method, path)

    def close(self):
        for listener in self._listeners:
            listener.call_method(listener.close)

    def _get_start_attrs(self, item, *extra):
        return self._get_attrs(item, self._start_attrs, extra)

    def _get_end_attrs(self, item, *extra):
        return self._get_attrs(item, self._end_attrs, extra)

    def _get_attrs(self, item, default, extra):
        names = self._get_attr_names(default, extra)
        return dict((n, self._get_attr_value(item, n)) for n in names)

    def _get_attr_names(self, default, extra):
        names = list(default)
        for name in extra:
            if not name.startswith('-'):
                names.append(name)
            elif name[1:] in names:
                names.remove(name[1:])
        return names

    def _get_attr_value(self, item, name):
        value = getattr(item, name)
        return self._take_copy_of_mutable_value(value)

    def _take_copy_of_mutable_value(self, value):
        if is_dict_like(value):
            return dict(value)
        if is_list_like(value):
            return list(value)
        return value


class ListenerProxy(AbstractLoggerProxy):
    _methods = ('start_suite', 'end_suite', 'start_test', 'end_test',
                'start_keyword', 'end_keyword', 'log_message', 'message',
                'output_file', 'report_file', 'log_file', 'debug_file',
                'xunit_file', 'close', 'library_import', 'resource_import',
                'variables_import')

    def __init__(self, listener):
        listener, name = self._import_listener(listener)
        AbstractLoggerProxy.__init__(self, listener)
        self.name = name
        self.version = self._get_version(listener)

    def _import_listener(self, listener):
        if not is_string(listener):
            return listener, type_name(listener)
        name, args = split_args_from_name_or_path(listener)
        importer = Importer('listener')
        listener = importer.import_class_or_module(os.path.normpath(name),
                                                   instantiate_with_args=args)
        return listener, name

    def _get_version(self, listener):
        try:
            version = int(listener.ROBOT_LISTENER_API_VERSION)
            if version != 2:
                raise ValueError
        except AttributeError:
            raise DataError("Listener '%s' does not have mandatory "
                            "'ROBOT_LISTENER_API_VERSION' attribute."
                            % self.name)
        except (ValueError, TypeError):
            raise DataError("Listener '%s' uses unsupported API version '%s'."
                            % (self.name, listener.ROBOT_LISTENER_API_VERSION))
        return version

    def call_method(self, method, *args):
        try:
            method(*args)
        except:
            message, details = get_error_details()
            LOGGER.error("Calling method '%s' of listener '%s' failed: %s"
                         % (method.__name__, self.name, message))
            LOGGER.info("Details:\n%s" % details)
