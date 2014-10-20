"""Raw data collector for Coverage."""

import sys
import inspect

from .test_finder import TestFinder, TestFinderResult


class PyTracer(object):
    """Python implementation of the raw data tracer."""

    # Because of poor implementations of trace-function-manipulating tools,
    # the Python trace function must be kept very simple.  In particular, there
    # must be only one function ever set as the trace function, both through
    # sys.settrace, and as the return value from the trace function.  Put
    # another way, the trace function must always return itself.  It cannot
    # swap in other functions, or return None to avoid tracing a particular
    # frame.
    #
    # The trace manipulator that introduced this restriction is DecoratorTools,
    # which sets a trace function, and then later restores the pre-existing one
    # by calling sys.settrace with a function it found in the current frame.
    #
    # Systems that use DecoratorTools (or similar trace manipulations) must use
    # PyTracer to get accurate results.  The command-line --timid argument is
    # used to force the use of this tracer.

    def __init__(self):
        # Attributes set from the collector:
        self.data = None
        self.callers_data = None
        self.test_ids = None
        self.arcs = False
        self.should_trace = None
        self.should_trace_cache = None
        self.should_record_callers = False
        self.warn = None
        self.plugin_data = None
        # The threading module to use, if any.
        self.threading = None

        self.plugin = []
        self.cur_file_dict = []
        self.cur_file_callers_dict = {}
        self.last_line = [0]

        self.data_stack = []
        self.last_exc_back = None
        self.last_exc_firstlineno = 0
        self.thread = None
        self.stopped = False

        self.test_finder = None

    def __repr__(self):
        return "<PyTracer at 0x{0:0x}: {1} lines in {2} files>".format(
            id(self),
            sum(len(v) for v in self.data.values()),
            len(self.data),
        )

    @staticmethod
    def _format_frame(frame):
        context_l = 1
        try:
            f_info = inspect.getframeinfo(frame, context=context_l)
            m_info = str(f_info.code_context[0]).strip()
        except IOError, e:
            m_info = str(e)
        return '%(m_info)s' % locals()

    @staticmethod
    def _format_call(frame):
        context_l = 0
        try:
            f_info = inspect.getframeinfo(frame, context=context_l)
            m_info = str(f_info)
        except IOError, e:
            m_info = str(e)
        return '%(m_info)s' % locals()

    def _trace(self, frame, event, arg_unused):
        """The trace function passed to sys.settrace."""

        if self.stopped:
            return

        if self.last_exc_back:            # TODO: bring this up to speed
            if frame == self.last_exc_back:
                # Someone forgot a return event.
                if self.arcs and self.cur_file_dict:
                    pair = (self.last_line, -self.last_exc_firstlineno)
                    self.cur_file_dict[pair] = None
                # TODO: do I need similar as above for self.cur_file_callers_dict ?
                self.plugin, self.cur_file_dict, self.cur_file_callers_dict, self.last_line = (
                    self.data_stack.pop()
                )
            self.last_exc_back = None

        filename = frame.f_code.co_filename

        DO_PRINT = False  # True

        if event == 'call':
            # Entering a new function context.  Decide if we should trace
            # in this file.
            self.data_stack.append(
                (self.plugin, self.cur_file_dict, self.cur_file_callers_dict, self.last_line)
            )
            # filename = frame.f_code.co_filename
            disp = self.should_trace_cache.get(filename)
            if disp is None:
                disp = self.should_trace(filename, frame)
                self.should_trace_cache[filename] = disp

            self.plugin = None
            self.cur_file_dict = None
            self.cur_file_callers_dict = None
            if disp.trace:
                tracename = disp.source_filename
                if DO_PRINT:
                    print("%s: %s - frame: <%s>" % (event, tracename, self._format_call(frame)))
                if disp.plugin:
                    dyn_func = disp.plugin.dynamic_source_file_name()
                    if dyn_func:
                        tracename = dyn_func(tracename, frame)
                        if tracename:
                            if not self.check_include(tracename):
                                tracename = None
            else:
                tracename = None
            if tracename:
                if tracename not in self.data:
                    self.data[tracename] = {}
                    if disp.plugin:
                        self.plugin_data[tracename] = disp.plugin.__name__
                if tracename not in self.callers_data:
                    self.callers_data[tracename] = {}
                self.cur_file_dict = self.data[tracename]
                self.cur_file_callers_dict = self.callers_data[tracename]
                self.plugin = disp.plugin
            # Set the last_line to -1 because the next arc will be entering a
            # code block, indicated by (-1, n).
            self.last_line = -1
        elif event == 'line':
            # Record an executed line.
            if self.plugin:
                lineno_from, lineno_to = self.plugin.line_number_range(frame)
            else:
                lineno_from, lineno_to = frame.f_lineno, frame.f_lineno
            if lineno_from != -1:
                if self.cur_file_dict is not None:
                    if DO_PRINT:
                        print("line %s: %s-%s - %s" % (filename, lineno_from, lineno_to, self._format_frame(frame)))

                    which_tests = None
                    if self.should_record_callers and self.cur_file_callers_dict is not None:
                        which_tests = self.test_finder.find_tests_in_frame(frame)

                    if self.arcs:
                        line_key = (self.last_line, lineno_from)
                        self.cur_file_dict[
                            line_key
                        ] = None
                        if which_tests is not None:
                            which_tests = TestFinderResult.merge_into_dict(
                                self.cur_file_callers_dict,
                                line_key,
                                which_tests
                            )
                    else:
                        for lineno in range(lineno_from, lineno_to+1):
                            self.cur_file_dict[lineno] = None
                            if which_tests is not None:
                                which_tests = TestFinderResult.merge_into_dict(
                                    self.cur_file_callers_dict,
                                    lineno,
                                    which_tests
                                )

                self.last_line = lineno_to
        elif event == 'return':
            if DO_PRINT and self.cur_file_dict:
                print("return from %s: %s - %s" % (filename, self.last_line, self._format_frame(frame)))
            if self.arcs and self.cur_file_dict:
                first = frame.f_code.co_firstlineno
                self.cur_file_dict[(self.last_line, -first)] = None
            # Leaving this function, pop the filename stack.
            self.plugin, self.cur_file_dict, self.cur_file_callers_dict, self.last_line = (
                self.data_stack.pop()
            )
        elif event == 'exception':
            self.last_exc_back = frame.f_back
            self.last_exc_firstlineno = frame.f_code.co_firstlineno
        return self._trace

    def start(self):
        """Start this Tracer.

        Return a Python function suitable for use with sys.settrace().

        """
        self.test_finder = TestFinder()
        if self.threading:
            self.thread = self.threading.currentThread()
        sys.settrace(self._trace)
        return self._trace

    def stop(self):
        """Stop this Tracer."""
        self.stopped = True
        if self.threading and self.thread != self.threading.currentThread():
            # Called on a different thread than started us: we can't unhook
            # ourseves, but we've set the flag that we should stop, so we won't
            # do any more tracing.
            return

        if self.warn:
            if sys.gettrace() != self._trace:
                msg = "Trace function changed, measurement is likely wrong: %r"
                self.warn(msg % (sys.gettrace(),))

        sys.settrace(None)

    def get_stats(self):
        """Return a dictionary of statistics, or None."""
        return None
