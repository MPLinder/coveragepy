/* C-based Tracer for Coverage. */

#include "Python.h"
#include "compile.h"        /* in 2.3, this wasn't part of Python.h */
#include "eval.h"           /* or this. */
#include "structmember.h"
#include "frameobject.h"

/* Compile-time debugging helpers */
#undef WHAT_LOG         /* Define to log the WHAT params in the trace function. */
#undef TRACE_LOG        /* Define to log our bookkeeping. */
#undef COLLECT_STATS    /* Collect counters: stats are printed when tracer is stopped. */

#if COLLECT_STATS
#define STATS(x)        x
#else
#define STATS(x)
#endif

/* Py 2.x and 3.x compatibility */

#ifndef Py_TYPE
#define Py_TYPE(o)    (((PyObject*)(o))->ob_type)
#endif

#if PY_MAJOR_VERSION >= 3

#define MyText_Type         PyUnicode_Type
#define MyText_Check(o)     PyUnicode_Check(o)
#define MyText_AS_BYTES(o)  PyUnicode_AsASCIIString(o)
#define MyText_AS_STRING(o) PyBytes_AS_STRING(o)
#define MyInt_FromLong(l)   PyLong_FromLong(l)
#define MyInt_AsLong(o)     PyLong_AsLong(o)

#define MyType_HEAD_INIT    PyVarObject_HEAD_INIT(NULL, 0)

#else

#define MyText_Type         PyString_Type
#define MyText_Check(o)     PyString_Check(o)
#define MyText_AS_BYTES(o)  (Py_INCREF(o), o)
#define MyText_AS_STRING(o) PyString_AS_STRING(o)
#define MyInt_FromLong(l)   PyInt_FromLong(l)
#define MyInt_AsLong(o)     PyInt_AsLong(o)

#define MyType_HEAD_INIT    PyObject_HEAD_INIT(NULL)  0,

#endif /* Py3k */

/* The values returned to indicate ok or error. */
#define RET_OK      0
#define RET_ERROR   -1

/* An entry on the data stack.  For each call frame, we need to record the
    dictionary to capture data, and the last line number executed in that
    frame.
*/
typedef struct {
    /* The current file_data dictionary.  Borrowed. */
    PyObject * file_data;

    /* The line number of the last line recorded, for tracing arcs.
        -1 means there was no previous line, as when entering a code object.
    */
    int last_line;
} DataStackEntry;

/* A data stack is a dynamically allocated vector of DataStackEntry's. */
typedef struct {
    int depth;      /* The index of the last-used entry in stack. */
    int alloc;      /* number of entries allocated at stack. */
    /* The file data at each level, or NULL if not recording. */
    DataStackEntry * stack;
} DataStack;

/* The CTracer type. */

typedef struct {
    PyObject_HEAD

    /* Python objects manipulated directly by the Collector class. */
    PyObject * should_trace;
    PyObject * warn;
    PyObject * coroutine_id_func;
    PyObject * data;
    PyObject * callers_data;
    PyObject * plugin_data;
    PyObject * should_trace_cache;
    PyObject * should_record_callers;
    PyObject * arcs;

    /* Has the tracer been started? */
    int started;
    /* Are we tracing arcs, or just lines? */
    int tracing_arcs;

    /*
        The data stack is a stack of dictionaries.  Each dictionary collects
        data for a single source file.  The data stack parallels the call stack:
        each call pushes the new frame's file data onto the data stack, and each
        return pops file data off.

        The file data is a dictionary whose form depends on the tracing options.
        If tracing arcs, the keys are line number pairs.  If not tracing arcs,
        the keys are line numbers.  In both cases, the value is irrelevant
        (None).
    */

    DataStack data_stack;       /* Used if we aren't doing coroutines. */
    PyObject * data_stack_index;     /* Used if we are doing coroutines. */
    DataStack * data_stacks;
    int data_stacks_alloc;
    int data_stacks_used;

    DataStack * pdata_stack;

    /* The current file's data stack entry, copied from the stack. */
    DataStackEntry cur_entry;

    /* The parent frame for the last exception event, to fix missing returns. */
    PyFrameObject * last_exc_back;
    int last_exc_firstlineno;

#if COLLECT_STATS
    struct {
        unsigned int calls;
        unsigned int lines;
        unsigned int returns;
        unsigned int exceptions;
        unsigned int others;
        unsigned int new_files;
        unsigned int missed_returns;
        unsigned int stack_reallocs;
        unsigned int errors;
    } stats;
#endif /* COLLECT_STATS */
} CTracer;


#define STACK_DELTA    100

static int
DataStack_init(CTracer *self, DataStack *pdata_stack)
{
    pdata_stack->depth = -1;
    pdata_stack->stack = NULL;
    pdata_stack->alloc = 0;
    return RET_OK;
}

static void
DataStack_dealloc(CTracer *self, DataStack *pdata_stack)
{
    PyMem_Free(pdata_stack->stack);
}

static int
DataStack_grow(CTracer *self, DataStack *pdata_stack)
{
    pdata_stack->depth++;
    if (pdata_stack->depth >= pdata_stack->alloc) {
        STATS( self->stats.stack_reallocs++; )
        /* We've outgrown our data_stack array: make it bigger. */
        int bigger = pdata_stack->alloc + STACK_DELTA;
        DataStackEntry * bigger_data_stack = PyMem_Realloc(pdata_stack->stack, bigger * sizeof(DataStackEntry));
        if (bigger_data_stack == NULL) {
            STATS( self->stats.errors++; )
            PyErr_NoMemory();
            pdata_stack->depth--;
            return RET_ERROR;
        }
        pdata_stack->stack = bigger_data_stack;
        pdata_stack->alloc = bigger;
    }
    return RET_OK;
}


static int
CTracer_init(CTracer *self, PyObject *args_unused, PyObject *kwds_unused)
{
#if COLLECT_STATS
    self->stats.calls = 0;
    self->stats.lines = 0;
    self->stats.returns = 0;
    self->stats.exceptions = 0;
    self->stats.others = 0;
    self->stats.new_files = 0;
    self->stats.missed_returns = 0;
    self->stats.stack_reallocs = 0;
    self->stats.errors = 0;
#endif /* COLLECT_STATS */

    self->should_trace = NULL;
    self->warn = NULL;
    self->coroutine_id_func = NULL;
    self->data = NULL;
    self->plugin_data = NULL;
    self->should_trace_cache = NULL;
    self->should_record_callers = NULL;
    self->arcs = NULL;

    self->started = 0;
    self->tracing_arcs = 0;

    if (DataStack_init(self, &self->data_stack)) {
        return RET_ERROR;
    }
    self->data_stack_index = PyDict_New();
    if (self->data_stack_index == NULL) {
        STATS( self->stats.errors++; )
        return RET_ERROR;
    }

    self->data_stacks = NULL;
    self->data_stacks_alloc = 0;
    self->data_stacks_used = 0;

    self->pdata_stack = &self->data_stack;

    self->cur_entry.file_data = NULL;
    self->cur_entry.last_line = -1;

    self->last_exc_back = NULL;

    return RET_OK;
}

static void
CTracer_dealloc(CTracer *self)
{
    int i;

    if (self->started) {
        PyEval_SetTrace(NULL, NULL);
    }

    Py_XDECREF(self->should_trace);
    Py_XDECREF(self->warn);
    Py_XDECREF(self->coroutine_id_func);
    Py_XDECREF(self->data);
    Py_XDECREF(self->callers_data);
    Py_XDECREF(self->plugin_data);
    Py_XDECREF(self->should_trace_cache);
    Py_XDECREF(self->should_record_callers);

    DataStack_dealloc(self, &self->data_stack);
    if (self->data_stacks) {
        for (i = 0; i < self->data_stacks_used; i++) {
            DataStack_dealloc(self, self->data_stacks + i);
        }
        PyMem_Free(self->data_stacks);
    }

    Py_XDECREF(self->data_stack_index);

    Py_TYPE(self)->tp_free((PyObject*)self);
}

#if TRACE_LOG
static const char *
indent(int n)
{
    static const char * spaces =
        "                                                                    "
        "                                                                    "
        "                                                                    "
        "                                                                    "
        ;
    return spaces + strlen(spaces) - n*2;
}

static int logging = 0;
/* Set these constants to be a file substring and line number to start logging. */
static const char * start_file = "tests/views";
static int start_line = 27;

static void
showlog(int depth, int lineno, PyObject * filename, const char * msg)
{
    if (logging) {
        printf("%s%3d ", indent(depth), depth);
        if (lineno) {
            printf("%4d", lineno);
        }
        else {
            printf("    ");
        }
        if (filename) {
            PyObject *ascii = MyText_AS_BYTES(filename);
            printf(" %s", MyText_AS_STRING(ascii));
            Py_DECREF(ascii);
        }
        if (msg) {
            printf(" %s", msg);
        }
        printf("\n");
    }
}

#define SHOWLOG(a,b,c,d)    showlog(a,b,c,d)
#else
#define SHOWLOG(a,b,c,d)
#endif /* TRACE_LOG */

#if WHAT_LOG
static const char * what_sym[] = {"CALL", "EXC ", "LINE", "RET "};
#endif

/* Record a pair of integers in self->cur_entry.file_data. */
static int
CTracer_record_pair(CTracer *self, int l1, int l2)
{
    int ret = RET_OK;

    PyObject * t = Py_BuildValue("(ii)", l1, l2);
    if (t != NULL) {
        if (PyDict_SetItem(self->cur_entry.file_data, t, Py_None) < 0) {
            STATS( self->stats.errors++; )
            ret = RET_ERROR;
        }
        Py_DECREF(t);
    }
    else {
        STATS( self->stats.errors++; )
        ret = RET_ERROR;
    }
    return ret;
}

/* Set self->pdata_stack to the proper data_stack to use. */
static int
CTracer_set_pdata_stack(CTracer *self)
{
    if (self->coroutine_id_func != Py_None) {
        PyObject * co_obj = NULL;
        PyObject * stack_index = NULL;
        long the_index = 0;

        co_obj = PyObject_CallObject(self->coroutine_id_func, NULL);
        if (co_obj == NULL) {
            return RET_ERROR;
        }
        stack_index = PyDict_GetItem(self->data_stack_index, co_obj);
        if (stack_index == NULL) {
            /* A new coroutine object.  Make a new data stack. */
            the_index = self->data_stacks_used;
            stack_index = MyInt_FromLong(the_index);
            if (PyDict_SetItem(self->data_stack_index, co_obj, stack_index) < 0) {
                STATS( self->stats.errors++; )
                Py_XDECREF(co_obj);
                Py_XDECREF(stack_index);
                return RET_ERROR;
            }
            self->data_stacks_used++;
            if (self->data_stacks_used >= self->data_stacks_alloc) {
                int bigger = self->data_stacks_alloc + 10;
                DataStack * bigger_stacks = PyMem_Realloc(self->data_stacks, bigger * sizeof(DataStack));
                if (bigger_stacks == NULL) {
                    STATS( self->stats.errors++; )
                    PyErr_NoMemory();
                    Py_XDECREF(co_obj);
                    Py_XDECREF(stack_index);
                    return RET_ERROR;
                }
                self->data_stacks = bigger_stacks;
                self->data_stacks_alloc = bigger;
            }
            DataStack_init(self, &self->data_stacks[the_index]);
        }
        else {
            Py_INCREF(stack_index);
            the_index = MyInt_AsLong(stack_index);
        }

        self->pdata_stack = &self->data_stacks[the_index];

        Py_XDECREF(co_obj);
        Py_XDECREF(stack_index);
    }
    else {
        self->pdata_stack = &self->data_stack;
    }

    return RET_OK;
}

/*
 * The Trace Function
 */
static int
CTracer_trace(CTracer *self, PyFrameObject *frame, int what, PyObject *arg_unused)
{
    int ret = RET_OK;
    PyObject * filename = NULL;
    PyObject * tracename = NULL;
    PyObject * disposition = NULL;
    PyObject * disp_trace = NULL;
    #if WHAT_LOG || TRACE_LOG
    PyObject * ascii = NULL;
    #endif

    #if WHAT_LOG
    if (what <= sizeof(what_sym)/sizeof(const char *)) {
        ascii = MyText_AS_BYTES(frame->f_code->co_filename);
        printf("trace: %s @ %s %d\n", what_sym[what], MyText_AS_STRING(ascii), frame->f_lineno);
        Py_DECREF(ascii);
    }
    #endif

    #if TRACE_LOG
    ascii = MyText_AS_BYTES(frame->f_code->co_filename);
    if (strstr(MyText_AS_STRING(ascii), start_file) && frame->f_lineno == start_line) {
        logging = 1;
    }
    Py_DECREF(ascii);
    #endif

    /* See below for details on missing-return detection. */
    if (self->last_exc_back) {
        if (frame == self->last_exc_back) {
            /* Looks like someone forgot to send a return event. We'll clear
               the exception state and do the RETURN code here.  Notice that the
               frame we have in hand here is not the correct frame for the RETURN,
               that frame is gone.  Our handling for RETURN doesn't need the
               actual frame, but we do log it, so that will look a little off if
               you're looking at the detailed log.

               If someday we need to examine the frame when doing RETURN, then
               we'll need to keep more of the missed frame's state.
            */
            STATS( self->stats.missed_returns++; )
            if (CTracer_set_pdata_stack(self)) {
                return RET_ERROR;
            }
            if (self->pdata_stack->depth >= 0) {
                if (self->tracing_arcs && self->cur_entry.file_data) {
                    if (CTracer_record_pair(self, self->cur_entry.last_line, -self->last_exc_firstlineno) < 0) {
                        return RET_ERROR;
                    }
                }
                SHOWLOG(self->pdata_stack->depth, frame->f_lineno, frame->f_code->co_filename, "missedreturn");
                self->cur_entry = self->pdata_stack->stack[self->pdata_stack->depth];
                self->pdata_stack->depth--;
            }
        }
        self->last_exc_back = NULL;
    }


    switch (what) {
    case PyTrace_CALL:      /* 0 */
        STATS( self->stats.calls++; )
        /* Grow the stack. */
        if (CTracer_set_pdata_stack(self)) {
            return RET_ERROR;
        }
        if (DataStack_grow(self, self->pdata_stack)) {
            return RET_ERROR;
        }

        /* Push the current state on the stack. */
        self->pdata_stack->stack[self->pdata_stack->depth] = self->cur_entry;

        /* Check if we should trace this line. */
        filename = frame->f_code->co_filename;
        disposition = PyDict_GetItem(self->should_trace_cache, filename);
        if (disposition == NULL) {
            STATS( self->stats.new_files++; )
            /* We've never considered this file before. */
            /* Ask should_trace about it. */
            PyObject * args = Py_BuildValue("(OO)", filename, frame);
            disposition = PyObject_Call(self->should_trace, args, NULL);
            Py_DECREF(args);
            if (disposition == NULL) {
                /* An error occurred inside should_trace. */
                STATS( self->stats.errors++; )
                return RET_ERROR;
            }
            if (PyDict_SetItem(self->should_trace_cache, filename, disposition) < 0) {
                STATS( self->stats.errors++; )
                return RET_ERROR;
            }
        }
        else {
            Py_INCREF(disposition);
        }

        disp_trace = PyObject_GetAttrString(disposition, "trace");
        if (disp_trace == NULL) {
            STATS( self->stats.errors++; )
            Py_DECREF(disposition);
            return RET_ERROR;
        }

        tracename = Py_None;
        Py_INCREF(tracename);

        if (disp_trace == Py_True) {
            /* If tracename is a string, then we're supposed to trace. */
            tracename = PyObject_GetAttrString(disposition, "source_filename");
            if (tracename == NULL) {
                STATS( self->stats.errors++; )
                Py_DECREF(disposition);
                Py_DECREF(disp_trace);
                return RET_ERROR;
            }
        }
        Py_DECREF(disp_trace);

        if (MyText_Check(tracename)) {
            PyObject * file_data = PyDict_GetItem(self->data, tracename);
            PyObject * disp_plugin = NULL;
            PyObject * disp_plugin_name = NULL;

            if (file_data == NULL) {
                file_data = PyDict_New();
                if (file_data == NULL) {
                    STATS( self->stats.errors++; )
                    Py_DECREF(tracename);
                    Py_DECREF(disposition);
                    return RET_ERROR;
                }
                ret = PyDict_SetItem(self->data, tracename, file_data);
                Py_DECREF(file_data);
                if (ret < 0) {
                    STATS( self->stats.errors++; )
                    Py_DECREF(tracename);
                    Py_DECREF(disposition);
                    return RET_ERROR;
                }

                if (self->plugin_data != NULL) {
                    /* If the disposition mentions a plugin, record that. */
                    disp_plugin = PyObject_GetAttrString(disposition, "plugin");
                    if (disp_plugin == NULL) {
                        STATS( self->stats.errors++; )
                        Py_DECREF(tracename);
                        Py_DECREF(disposition);
                        return RET_ERROR;
                    }
                    if (disp_plugin != Py_None) {
                        disp_plugin_name = PyObject_GetAttrString(disp_plugin, "__name__");
                        Py_DECREF(disp_plugin);
                        if (disp_plugin_name == NULL) {
                            STATS( self->stats.errors++; )
                            Py_DECREF(tracename);
                            Py_DECREF(disposition);
                            return RET_ERROR;
                        }
                        ret = PyDict_SetItem(self->plugin_data, tracename, disp_plugin_name);
                        Py_DECREF(disp_plugin_name);
                        if (ret < 0) {
                            Py_DECREF(tracename);
                            Py_DECREF(disposition);
                            return RET_ERROR;
                        }
                    }
                }
            }
            self->cur_entry.file_data = file_data;
            /* Make the frame right in case settrace(gettrace()) happens. */
            Py_INCREF(self);
            frame->f_trace = (PyObject*)self;
            SHOWLOG(self->pdata_stack->depth, frame->f_lineno, filename, "traced");
        }
        else {
            self->cur_entry.file_data = NULL;
            SHOWLOG(self->pdata_stack->depth, frame->f_lineno, filename, "skipped");
        }

        Py_DECREF(tracename);
        Py_DECREF(disposition);

        self->cur_entry.last_line = -1;
        break;

    case PyTrace_RETURN:    /* 3 */
        STATS( self->stats.returns++; )
        /* A near-copy of this code is above in the missing-return handler. */
        if (CTracer_set_pdata_stack(self)) {
            return RET_ERROR;
        }
        if (self->pdata_stack->depth >= 0) {
            if (self->tracing_arcs && self->cur_entry.file_data) {
                int first = frame->f_code->co_firstlineno;
                if (CTracer_record_pair(self, self->cur_entry.last_line, -first) < 0) {
                    return RET_ERROR;
                }
            }

            SHOWLOG(self->pdata_stack->depth, frame->f_lineno, frame->f_code->co_filename, "return");
            self->cur_entry = self->pdata_stack->stack[self->pdata_stack->depth];
            self->pdata_stack->depth--;
        }
        break;

    case PyTrace_LINE:      /* 2 */
        STATS( self->stats.lines++; )
        if (self->pdata_stack->depth >= 0) {
            SHOWLOG(self->pdata_stack->depth, frame->f_lineno, frame->f_code->co_filename, "line");
            if (self->cur_entry.file_data) {
                /* We're tracing in this frame: record something. */
                if (self->tracing_arcs) {
                    /* Tracing arcs: key is (last_line,this_line). */
                    if (CTracer_record_pair(self, self->cur_entry.last_line, frame->f_lineno) < 0) {
                        return RET_ERROR;
                    }
                }
                else {
                    /* Tracing lines: key is simply this_line. */
                    PyObject * this_line = MyInt_FromLong(frame->f_lineno);
                    if (this_line == NULL) {
                        STATS( self->stats.errors++; )
                        return RET_ERROR;
                    }
                    ret = PyDict_SetItem(self->cur_entry.file_data, this_line, Py_None);
                    Py_DECREF(this_line);
                    if (ret < 0) {
                        STATS( self->stats.errors++; )
                        return RET_ERROR;
                    }
                }
            }
            self->cur_entry.last_line = frame->f_lineno;
        }
        break;

    case PyTrace_EXCEPTION:
        /* Some code (Python 2.3, and pyexpat anywhere) fires an exception event
           without a return event.  To detect that, we'll keep a copy of the
           parent frame for an exception event.  If the next event is in that
           frame, then we must have returned without a return event.  We can
           synthesize the missing event then.

           Python itself fixed this problem in 2.4.  Pyexpat still has the bug.
           I've reported the problem with pyexpat as http://bugs.python.org/issue6359 .
           If it gets fixed, this code should still work properly.  Maybe some day
           the bug will be fixed everywhere coverage.py is supported, and we can
           remove this missing-return detection.

           More about this fix: http://nedbatchelder.com/blog/200907/a_nasty_little_bug.html
        */
        STATS( self->stats.exceptions++; )
        self->last_exc_back = frame->f_back;
        self->last_exc_firstlineno = frame->f_code->co_firstlineno;
        break;

    default:
        STATS( self->stats.others++; )
        break;
    }

    return RET_OK;
}

/*
 * Python has two ways to set the trace function: sys.settrace(fn), which
 * takes a Python callable, and PyEval_SetTrace(func, obj), which takes
 * a C function and a Python object.  The way these work together is that
 * sys.settrace(pyfn) calls PyEval_SetTrace(builtin_func, pyfn), using the
 * Python callable as the object in PyEval_SetTrace.  So sys.gettrace()
 * simply returns the Python object used as the second argument to
 * PyEval_SetTrace.  So sys.gettrace() will return our self parameter, which
 * means it must be callable to be used in sys.settrace().
 *
 * So we make our self callable, equivalent to invoking our trace function.
 *
 * To help with the process of replaying stored frames, this function has an
 * optional keyword argument:
 *
 *      def CTracer_call(frame, event, arg, lineno=0)
 *
 * If provided, the lineno argument is used as the line number, and the
 * frame's f_lineno member is ignored.
 */
static PyObject *
CTracer_call(CTracer *self, PyObject *args, PyObject *kwds)
{
    PyFrameObject *frame;
    PyObject *what_str;
    PyObject *arg;
    int lineno = 0;
    int what;
    int orig_lineno;
    PyObject *ret = NULL;

    static char *what_names[] = {
        "call", "exception", "line", "return",
        "c_call", "c_exception", "c_return",
        NULL
        };

    #if WHAT_LOG
    printf("pytrace\n");
    #endif

    static char *kwlist[] = {"frame", "event", "arg", "lineno", NULL};

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "O!O!O|i:Tracer_call", kwlist,
            &PyFrame_Type, &frame, &MyText_Type, &what_str, &arg, &lineno)) {
        goto done;
    }

    /* In Python, the what argument is a string, we need to find an int
       for the C function. */
    for (what = 0; what_names[what]; what++) {
        PyObject *ascii = MyText_AS_BYTES(what_str);
        int should_break = !strcmp(MyText_AS_STRING(ascii), what_names[what]);
        Py_DECREF(ascii);
        if (should_break) {
            break;
        }
    }

    /* Save off the frame's lineno, and use the forced one, if provided. */
    orig_lineno = frame->f_lineno;
    if (lineno > 0) {
        frame->f_lineno = lineno;
    }

    /* Invoke the C function, and return ourselves. */
    if (CTracer_trace(self, frame, what, arg) == RET_OK) {
        Py_INCREF(self);
        ret = (PyObject *)self;
    }

    /* Clean up. */
    frame->f_lineno = orig_lineno;

done:
    return ret;
}

static PyObject *
CTracer_start(CTracer *self, PyObject *args_unused)
{
    PyEval_SetTrace((Py_tracefunc)CTracer_trace, (PyObject*)self);
    self->started = 1;
    self->tracing_arcs = self->arcs && PyObject_IsTrue(self->arcs);
    self->cur_entry.last_line = -1;

    /* start() returns a trace function usable with sys.settrace() */
    Py_INCREF(self);
    return (PyObject *)self;
}

static PyObject *
CTracer_stop(CTracer *self, PyObject *args_unused)
{
    if (self->started) {
        PyEval_SetTrace(NULL, NULL);
        self->started = 0;
    }

    return Py_BuildValue("");
}

static PyObject *
CTracer_get_stats(CTracer *self)
{
#if COLLECT_STATS
    return Py_BuildValue(
        "{sI,sI,sI,sI,sI,sI,sI,sI,si,sI}",
        "calls", self->stats.calls,
        "lines", self->stats.lines,
        "returns", self->stats.returns,
        "exceptions", self->stats.exceptions,
        "others", self->stats.others,
        "new_files", self->stats.new_files,
        "missed_returns", self->stats.missed_returns,
        "stack_reallocs", self->stats.stack_reallocs,
        "stack_alloc", self->pdata_stack->alloc,
        "errors", self->stats.errors
        );
#else
    return Py_BuildValue("");
#endif /* COLLECT_STATS */
}

static PyMemberDef
CTracer_members[] = {
    { "should_trace",       T_OBJECT, offsetof(CTracer, should_trace), 0,
            PyDoc_STR("Function indicating whether to trace a file.") },

    { "warn",               T_OBJECT, offsetof(CTracer, warn), 0,
            PyDoc_STR("Function for issuing warnings.") },

    { "coroutine_id_func",  T_OBJECT, offsetof(CTracer, coroutine_id_func), 0,
            PyDoc_STR("Function for determining coroutine context") },

    { "data",               T_OBJECT, offsetof(CTracer, data), 0,
            PyDoc_STR("The raw dictionary of trace data.") },

    { "callers_data",               T_OBJECT, offsetof(CTracer, callers_data), 0,
            PyDoc_STR("The raw dictionary of test callers data.") },

    { "plugin_data",        T_OBJECT, offsetof(CTracer, plugin_data), 0,
            PyDoc_STR("Mapping from filename to plugin name.") },

    { "should_trace_cache", T_OBJECT, offsetof(CTracer, should_trace_cache), 0,
            PyDoc_STR("Dictionary caching should_trace results.") },

    { "should_record_callers", T_OBJECT, offsetof(CTracer, should_record_callers), 0,
            PyDoc_STR("Function indicating whether we should record test callers.") },

    { "arcs",               T_OBJECT, offsetof(CTracer, arcs), 0,
            PyDoc_STR("Should we trace arcs, or just lines?") },

    { NULL }
};

static PyMethodDef
CTracer_methods[] = {
    { "start",      (PyCFunction) CTracer_start,        METH_VARARGS,
            PyDoc_STR("Start the tracer") },

    { "stop",       (PyCFunction) CTracer_stop,         METH_VARARGS,
            PyDoc_STR("Stop the tracer") },

    { "get_stats",  (PyCFunction) CTracer_get_stats,    METH_VARARGS,
            PyDoc_STR("Get statistics about the tracing") },

    { NULL }
};

static PyTypeObject
CTracerType = {
    MyType_HEAD_INIT
    "coverage.CTracer",        /*tp_name*/
    sizeof(CTracer),           /*tp_basicsize*/
    0,                         /*tp_itemsize*/
    (destructor)CTracer_dealloc, /*tp_dealloc*/
    0,                         /*tp_print*/
    0,                         /*tp_getattr*/
    0,                         /*tp_setattr*/
    0,                         /*tp_compare*/
    0,                         /*tp_repr*/
    0,                         /*tp_as_number*/
    0,                         /*tp_as_sequence*/
    0,                         /*tp_as_mapping*/
    0,                         /*tp_hash */
    (ternaryfunc)CTracer_call, /*tp_call*/
    0,                         /*tp_str*/
    0,                         /*tp_getattro*/
    0,                         /*tp_setattro*/
    0,                         /*tp_as_buffer*/
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE, /*tp_flags*/
    "CTracer objects",         /* tp_doc */
    0,                         /* tp_traverse */
    0,                         /* tp_clear */
    0,                         /* tp_richcompare */
    0,                         /* tp_weaklistoffset */
    0,                         /* tp_iter */
    0,                         /* tp_iternext */
    CTracer_methods,           /* tp_methods */
    CTracer_members,           /* tp_members */
    0,                         /* tp_getset */
    0,                         /* tp_base */
    0,                         /* tp_dict */
    0,                         /* tp_descr_get */
    0,                         /* tp_descr_set */
    0,                         /* tp_dictoffset */
    (initproc)CTracer_init,    /* tp_init */
    0,                         /* tp_alloc */
    0,                         /* tp_new */
};

/* Module definition */

#define MODULE_DOC PyDoc_STR("Fast coverage tracer.")

#if PY_MAJOR_VERSION >= 3

static PyModuleDef
moduledef = {
    PyModuleDef_HEAD_INIT,
    "coverage.tracer",
    MODULE_DOC,
    -1,
    NULL,       /* methods */
    NULL,
    NULL,       /* traverse */
    NULL,       /* clear */
    NULL
};


PyObject *
PyInit_tracer(void)
{
    PyObject * mod = PyModule_Create(&moduledef);
    if (mod == NULL) {
        return NULL;
    }

    CTracerType.tp_new = PyType_GenericNew;
    if (PyType_Ready(&CTracerType) < 0) {
        Py_DECREF(mod);
        return NULL;
    }

    Py_INCREF(&CTracerType);
    PyModule_AddObject(mod, "CTracer", (PyObject *)&CTracerType);

    return mod;
}

#else

void
inittracer(void)
{
    PyObject * mod;

    mod = Py_InitModule3("coverage.tracer", NULL, MODULE_DOC);
    if (mod == NULL) {
        return;
    }

    CTracerType.tp_new = PyType_GenericNew;
    if (PyType_Ready(&CTracerType) < 0) {
        return;
    }

    Py_INCREF(&CTracerType);
    PyModule_AddObject(mod, "CTracer", (PyObject *)&CTracerType);
}

#endif /* Py3k */
