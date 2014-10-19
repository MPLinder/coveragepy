#!/usr/bin/env python
# -*- coding: utf-8; mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vim: fileencoding=utf-8 tabstop=4 expandtab shiftwidth=4

"""
Code to find test callers in the stack when tracing statements.
"""

import os
import inspect
from collections import namedtuple
from unittest import TestCase, FunctionTestCase
from pprint import pformat

VERBOSE_LEVEL = 1


def log(msg, level=1):
    if level >= VERBOSE_LEVEL:
        print(msg)


TestIdentifier = namedtuple("TestIdentifier", ['filename', 'line_no', 'function_name'])


class TestFinder(object):
    """
    A class used by the tracer (pytracer.py only currently) to identify
    calling test functions when tracing statements by looking at the stack.
    """
    def __init__(self, test_ids):
        # TODO: build this dynamically by trying to import
        # various suites and obtain their classes...

        self.test_case_classes = tuple([TestCase, FunctionTestCase])

        # Used to generate short integer IDs for tests.
        self._current_test_num = 0

        # Map the full test ID to a short integer ID.
        self.test_ids = test_ids
        return

    def find_tests_in_frame(self, trace_frame):
        """Identify anything that looks like a 'test' in the call stack."""

        test_methods = set()

        trace_frame_info = inspect.getframeinfo(trace_frame)

        # Is this the right concept to use to identify these?
        trace_frame_id = "%s:%s:%s" % (trace_frame_info.filename, trace_frame_info.lineno, trace_frame_info.function)

        frame = trace_frame
        i = 0
        while True:
            i += 1
            frame = getattr(frame, "f_back", None)
            if not frame:
                break

            f_info = inspect.getframeinfo(frame)
            obj_name = f_info.function
            # m_info = str(f_info.code_context[0]).strip()
            #
            # this_self = None
            # arg_vals = inspect.getargvalues(frame)
            # if arg_vals and len(arg_vals.args):
            #     first_arg = arg_vals.args[0]
            #     this_self = arg_vals.locals[first_arg]

            is_test_method = self._is_test_method(frame, f_info)

            if is_test_method:
                # test_method_label = "%s:%s:%s" % (f_info.filename, f_info.lineno, obj_name)
                test_id = self.get_test_id(f_info.filename, f_info.lineno, obj_name)
                test_methods.add(test_id)
                # print("%s - %s %r (%s) %s" % (i, obj_name, this_self, test_method_label, m_info))

        which_tests = TestFinderResult(trace_frame_id, test_methods)
        return which_tests

    def _is_test_method(self, frame, frame_info):
        obj_name = frame_info.function

        # If it's a function simply named 'test', or begins
        # with 'test_', then assume it's a valid test function
        if obj_name == 'test' or obj_name.find('test_') > -1:
            return True

        # Find the first argument to the function, if any,
        # to see if it looks like an instance of a test case class.
        this_self = None
        arg_vals = inspect.getargvalues(frame)
        if arg_vals and len(arg_vals.args):
            first_arg = arg_vals.args[0]
            this_self = arg_vals.locals[first_arg]

        if this_self:

            # Does this look like a method of a known test case class?
            if isinstance(this_self, self.test_case_classes):
                # If the function call looks like it originates
                # in the unit test framework itself, do not include.
                if not self.is_test_framework_method(frame, frame_info):
                    return True

        return False

    # noinspection PyUnusedLocal
    @staticmethod
    def is_test_framework_method(frame, frame_info):
        """
        :return: True if the function at this frame appears
        to live inside a unit test framework.

        This need some work to be more flexible with different frameworks...
        """
        fname = frame_info.filename
        if fname.find(os.sep + "unittest" + os.sep) != -1:
            return True
        return False

    def get_test_id(self, source_file, line_no, func_name):
        #full_id = "%s:%s:%s" % (source_file, line_no, func_name)
        full_id = TestIdentifier(
            filename=source_file,
            line_no=line_no,
            function_name=func_name,
        )
        short_id = self.test_ids.get(full_id, None)
        if short_id is not None:
            return short_id
        self._current_test_num += 1
        self.test_ids[full_id] = self._current_test_num
        return self._current_test_num

    def get_test_info_for_id(self, short_id):
        """
        Get a description of a calling test using the short ID.
        If the short_id is unknown we raise ValueError.

        :return: a named tuple describing a previously encountered test, given by the short ID.
        """
        for key, val in self.test_ids.iteritems():
            if val == short_id:
                return key
        raise ValueError("Test ID %s not found." % (short_id,))


class TestFinderResult(object):
    """
    Contains the results of looking for tests in the call stack of a single
    line under test (LUT).

    line_id is an identifier for the LUT

    test_methods is a set of identifiers for identified tests.

    Those are implemented as simple integers, which can be converted to a full
    ID with TestFinder.get_test_info_for_id(short_id)
    """

    def __init__(self, line_id, test_methods=None):
        self.test_methods = test_methods or set()
        self.line_id = line_id

    def has_tests(self):
        if self.test_methods and len(self.test_methods):
            return True
        return False

    def __str__(self):
        if not self.has_tests():
            return "(None)"
        return "\n(%s ==> %s)" % \
               (self.line_id, pformat(self.test_methods))

    def __repr__(self):
        return self.__str__()

    def merge(self, other_result):
        """Merge another TestFinderResult that has the same line_id
        into this one, and return the modified instance."""
        if self.line_id != other_result.line_id:
            raise ValueError("Cannot merge results from different line_id's.")
        self.test_methods = self.test_methods.union(other_result.test_methods)
        return self

    @staticmethod
    def merge_into_dict(lines_dict, dict_key, result):
        """
        Combine a TestFinderResult for the current statement execution
        with any past TestFinderResult for that same statement using
        the given dictionary and key.

        :param lines_dict: cur_file_callers_dict from pytracer
        :param dict_key: either an arc pair or the line number from the file under test
        :param result: the TestFinderResult instance from the current statement
        :return: the merged TestFinderResult
        """
        if not dict_key:
            raise ValueError("dict_key param cannot be false")
        if result is None:
            raise ValueError("result param cannot be None")
        past_result = lines_dict.get(dict_key, None)
        if past_result is not None:
            past_result.merge(result)
        else:
            past_result = result
        if not past_result.has_tests():
            past_result = None
        lines_dict[dict_key] = past_result
        return past_result
