#!/usr/bin/env python
# -*- coding: utf-8; mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vim: fileencoding=utf-8 tabstop=4 expandtab shiftwidth=4

"""
Test the 'which tests covered this line' stuff...

I'm running this from PyCharm with a command line:

Script:
D:\PycharmWorkspace\coveragepy\venv\Scripts\coverage2-script.py

Arguments:
run --branch tests\test_whichtests.py

and then observing the debug statements printed at the console...
"""

from unittest import TestCase

T = True
F = False


def foo(a, b, c):
    return (a and b) or c


def bar(a, b, c):
    return not a and (b and c)


class WhichTestsCovered(TestCase):
    def test_one(self):
        self.assertTrue(foo(T, F, T))
        return

    def test_two(self):
        self.assertFalse(bar(T, T, T))
        self.assertTrue(bar(F, T, T))
        return

    def test_three(self):
        self.do_something()
        self.assertFalse(bar(T, T, T))
        return

    def do_something(self):
        # print("do_something")
        self.assertFalse(foo(T, F, F))
        return

if __name__ == "__main__":
    import unittest
    unittest.main()
