#!/usr/bin/env python
# -*- coding: utf-8; mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vim: fileencoding=utf-8 tabstop=4 expandtab shiftwidth=4

"""
Code to find test callers in the stack when tracing statements.
"""

import os
import sys

from inspect import istraceback, getfile
from collections import namedtuple
from unittest import TestCase, FunctionTestCase
from pprint import pformat

try:
    from nose.tools import nottest
except ImportError, e:
    def nottest(func):
        return func

from coverage.backward import iitems

VERBOSE_LEVEL = 1


def log(msg, level=1):
    if level >= VERBOSE_LEVEL:
        sys.stderr.write(msg)
        sys.stderr.write("\n")
        sys.stderr.flush()

#: Collect basic info about a frame: filename, line number, and function name
FrameInfo = namedtuple("FrameInfo", ['filename', 'line_no', 'function_name'])


class TestFinder(object):
    """
    A class used by the tracer (pytracer.py only currently) to identify
    calling test functions when tracing statements by looking at the stack.
    """
    def __init__(self):
        # TODO: build this dynamically by trying to import
        # various suites and obtain their classes...

        self.test_case_classes = tuple([TestCase, FunctionTestCase])

        # Identify package names of common unit test frameworks
        self._test_packages = [
            os.sep + 'unittest' + os.sep,
            os.sep + 'nose' + os.sep,
        ]

        return

    @nottest
    def is_test_method(self, frame, f_info):
        obj_name = f_info.function_name
        # log("Is test? " + str(f_info))

        # If it's a function simply named 'test', or begins
        # with 'test_', then assume it's a valid test function
        if obj_name == 'test' or obj_name.find('test_') > -1:
            return True

        #log(obj_name[:9])
        if f_info.filename[:9] == "<doctest ":
            # log("FOUND DT: " + f_info.filename)
            return True

        this_self = self.get_first_arg(frame)
        if this_self is not None:

            # Does this look like a method of a known test case class?
            if isinstance(this_self, self.test_case_classes):
                # If the function call looks like it originates
                # in the unit test framework itself, do not include.
                if not self._is_test_framework_method(frame, f_info):
                    return True

        return False

    @staticmethod
    def get_frame_info(frame):
        """Get a basic description of the frame. Faster than the version
        in inspect.py (which does more)

        :return: a basic description of the frame.
        :rtype: FrameInfo
        """
        if istraceback(frame):
            lineno = frame.tb_lineno
            frame = frame.tb_frame
        else:
            lineno = frame.f_lineno

        filename = getfile(frame)
        func_name = frame.f_code.co_name
        return FrameInfo(filename, lineno, func_name)

    @staticmethod
    def get_first_arg(frame):
        """Grab the first function/method argument, if any, from the frame."""
        co = frame.f_code
        varnames = co.co_varnames
        if len(varnames) < 1:
            return None

        return frame.f_locals.get(varnames[0], None)

    # noinspection PyUnusedLocal
    @nottest
    def _is_test_framework_method(self, frame, f_info):
        """
        :return: True if the function at this frame appears
        to live inside a unit test framework.

        This need some work to be more flexible with different frameworks...
        """
        for package_name in self._test_packages:
            if f_info.filename.find(package_name) != -1:
            # if f_info[0].find(package_name) != -1:
                return True
        return False

    @staticmethod
    def merge_callers_dicts(this_callers, other_callers):
        """
        Merge two callers dicts used by collector/tracer to record test callers.

        Each dict is assumed to follow the callers dict format documented in
        data.CoverageData - but is for a single file, (i.e, is an inner value dict)
        """

        # Both data sets have this file, so merge them.
        for line_or_arc, other_test_result in iitems(other_callers):

            # If the other line/arc is not in this file, add it and move on.
            this_test_result = this_callers.get(line_or_arc, None)
            if this_test_result is None:
                this_callers[line_or_arc] = other_test_result
                continue

            # This line/arc is present in both files; merge them.
            this_test_result.merge(other_test_result)

        return this_callers


class TestFinderResult(object):
    """
    Contains the results of looking for tests in the call stack of a single
    line under test (LUT).

    line_id is an identifier for the LUT
    TODO: line_id concept is not used.  Line associated is not kept in the result,
    but rather is a KEY where this object is a VALUE.

    test_methods is a set of FrameInfo's for tests we found.
    """

    def __init__(self, line_id, test_methods=None):
        self.test_methods = test_methods or set()
        self.line_id = line_id

    @nottest
    def has_tests(self):
        # if self.test_methods and len(self.test_methods):
        if self.test_methods:
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
        # if self.line_id != other_result.line_id:
        #     raise ValueError("Cannot merge results from different line_id's.\nID 1: %s\nID 2: %s\n" % (self.line_id, other_result.line_id))
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
        if not past_result.test_methods:
            past_result = None
        # Don't need to store None values
        if past_result is not None:
            lines_dict[dict_key] = past_result
        return past_result
