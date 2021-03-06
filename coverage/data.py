"""Coverage data for Coverage."""

import os

from coverage.backward import iitems, pickle
from coverage.files import PathAliases
from coverage.misc import file_be_gone
from coverage.test_finder import TestFinder

class CoverageData(object):
    """Manages collected coverage data, including file storage.

    The data file format is a pickled dict, with these keys:

        * collector: a string identifying the collecting software

        * lines: a dict mapping filenames to sorted lists of line numbers
          executed:
            { 'file1': [17,23,45],  'file2': [1,2,3], ... }

        * arcs: a dict mapping filenames to sorted lists of line number pairs:
            { 'file1': [(17,23), (17,25), (25,26)], ... }

        * plugins: a dict mapping filenames to plugin names:
            { 'file1': "django.coverage", ... }
            # TODO: how to handle the difference between a plugin module
            # name, and the class in the module?

        * callers: a dict mapping filenames to another dict that maps
            line numbers (or arcs) to a TestFinderResult, which contains a set of calling tests.
            Calling tests are identified with the TestIdentifier named tuple
            consisting of a filename, line number, and function name
            { 'file1':
                {
                   (-1, 50): TestFinderResult(set([TestIdentifier(
                        filename='tests\\mytests.py', line_no=44, function_name='test_three'
                      )])),
                },
              'file2: ...
            }

    """

    def __init__(self, basename=None, collector=None, debug=None):
        """Create a CoverageData.

        `basename` is the name of the file to use for storing data.

        `collector` is a string describing the coverage measurement software.

        `debug` is a `DebugControl` object for writing debug messages.

        """
        self.collector = collector or 'unknown'
        self.debug = debug

        self.use_file = True

        # Construct the filename that will be used for data file storage, if we
        # ever do any file storage.
        self.filename = basename or ".coverage"
        self.filename = os.path.abspath(self.filename)

        # A map from canonical Python source file name to a dictionary in
        # which there's an entry for each line number that has been
        # executed:
        #
        #   {
        #       'filename1.py': { 12: None, 47: None, ... },
        #       ...
        #       }
        #
        self.lines = {}

        # A map from canonical Python source file name to a dictionary with an
        # entry for each pair of line numbers forming an arc:
        #
        #   {
        #       'filename1.py': { (12,14): None, (47,48): None, ... },
        #       ...
        #       }
        #
        self.arcs = {}

        # A map from canonical source file name to an plugin module name:
        #
        #   {
        #       'filename1.py': 'django.coverage',
        #       ...
        #       }
        self.plugins = {}

        # A map from canonical source file name to a dict of test callers.
        # Value dict is a map of source line numbers (or arc pairs if in branch mode)
        # to a set of calling tests.
        #
        #  { 'file1':
        #       {
        #           (-1, 50): TestFinderResult(set([TestIdentifier(
        #                filename='tests\\mytests.py', line_no=44, function_name='test_three'
        #              )])),
        #       },
        #     'file2: ...
        #   }
        self.callers = {}

    def usefile(self, use_file=True):
        """Set whether or not to use a disk file for data."""
        self.use_file = use_file

    def read(self):
        """Read coverage data from the coverage data file (if it exists)."""
        if self.use_file:
            self.lines, self.arcs, self.callers = self._read_file(self.filename)
        else:
            self.lines, self.arcs, self.callers = {}, {}, {}

    def write(self, suffix=None):
        """Write the collected coverage data to a file.

        `suffix` is a suffix to append to the base file name. This can be used
        for multiple or parallel execution, so that many coverage data files
        can exist simultaneously.  A dot will be used to join the base name and
        the suffix.

        """
        if self.use_file:
            filename = self.filename
            if suffix:
                filename += "." + suffix
            self.write_file(filename)

    def erase(self):
        """Erase the data, both in this object, and from its file storage."""
        if self.use_file:
            if self.filename:
                file_be_gone(self.filename)
        self.lines = {}
        self.arcs = {}
        self.callers = {}

    def line_data(self):
        """Return the map from filenames to lists of line numbers executed."""
        return dict(
            (f, sorted(lmap.keys())) for f, lmap in iitems(self.lines)
            )

    def arc_data(self):
        """Return the map from filenames to lists of line number pairs."""
        return dict(
            (f, sorted(amap.keys())) for f, amap in iitems(self.arcs)
            )

    def callers_data(self):
        """Return the map from filenames to test caller maps."""
        return dict(
            (f, callers_map) for f, callers_map in iitems(self.callers)
            )

    def plugin_data(self):
        return self.plugins

    def write_file(self, filename):
        """Write the coverage data to `filename`."""

        # Create the file data.
        data = {}

        data['lines'] = self.line_data()
        arcs = self.arc_data()
        if arcs:
            data['arcs'] = arcs

        calling_tests = self.callers_data()
        if calling_tests:
            data['callers'] = calling_tests

        if self.collector:
            data['collector'] = self.collector

        if self.debug and self.debug.should('dataio'):
            self.debug.write("Writing data to %r" % (filename,))

        # Write the pickle to the file.
        with open(filename, 'wb') as fdata:
            pickle.dump(data, fdata, 2)

    def read_file(self, filename):
        """Read the coverage data from `filename`."""
        self.lines, self.arcs, self.callers = self._read_file(filename)

    def raw_data(self, filename):
        """Return the raw pickled data from `filename`."""
        if self.debug and self.debug.should('dataio'):
            self.debug.write("Reading data from %r" % (filename,))
        with open(filename, 'rb') as fdata:
            data = pickle.load(fdata)
        return data

    def _read_file(self, filename):
        """Return the stored coverage data from the given file.

        Returns three values, suitable for assigning to `self.lines` and
        `self.arcs` and `self.callers`.

        """
        lines = {}
        arcs = {}
        callers = {}
        try:
            data = self.raw_data(filename)
            if isinstance(data, dict):
                # Unpack the 'lines' item.
                lines = dict([
                    (f, dict.fromkeys(linenos, None))
                        for f, linenos in iitems(data.get('lines', {}))
                    ])
                # Unpack the 'arcs' item.
                arcs = dict([
                    (f, dict.fromkeys(arcpairs, None))
                        for f, arcpairs in iitems(data.get('arcs', {}))
                    ])
                # Unpack the 'callers' item (sets of calling tests).
                callers = dict([
                    (f, test_sets_map)
                        for f, test_sets_map in iitems(data.get('callers', {}))
                    ])
        except Exception:
            pass
        return lines, arcs, callers

    def combine_parallel_data(self, aliases=None):
        """Combine a number of data files together.

        Treat `self.filename` as a file prefix, and combine the data from all
        of the data files starting with that prefix plus a dot.

        If `aliases` is provided, it's a `PathAliases` object that is used to
        re-map paths to match the local machine's.

        """
        aliases = aliases or PathAliases()
        data_dir, local = os.path.split(self.filename)
        localdot = local + '.'
        for f in os.listdir(data_dir or '.'):
            if f.startswith(localdot):
                full_path = os.path.join(data_dir, f)
                new_lines, new_arcs, new_callers = self._read_file(full_path)
                for filename, file_data in iitems(new_lines):
                    filename = aliases.map(filename)
                    self.lines.setdefault(filename, {}).update(file_data)
                for filename, file_data in iitems(new_arcs):
                    filename = aliases.map(filename)
                    self.arcs.setdefault(filename, {}).update(file_data)

                for filename, file_data in iitems(new_callers):
                    filename = aliases.map(filename)
                    all_callers = self.callers.setdefault(filename, {})
                    TestFinder.merge_callers_dicts(all_callers, file_data)
                if f != local:
                    os.remove(full_path)

    def add_line_data(self, line_data):
        """Add executed line data.

        `line_data` is { filename: { lineno: None, ... }, ...}

        """
        for filename, linenos in iitems(line_data):
            self.lines.setdefault(filename, {}).update(linenos)

    def add_arc_data(self, arc_data):
        """Add measured arc data.

        `arc_data` is { filename: { (l1,l2): None, ... }, ...}

        """
        for filename, arcs in iitems(arc_data):
            self.arcs.setdefault(filename, {}).update(arcs)

    def add_callers_data(self, callers_data):
        """Add measured test callers data.
        """
        for filename, callers_dict in iitems(callers_data):
            self.callers.setdefault(filename, {}).update(callers_dict)

    def add_plugin_data(self, plugin_data):
        self.plugins.update(plugin_data)

    def touch_file(self, filename):
        """Ensure that `filename` appears in the data, empty if needed."""
        self.lines.setdefault(filename, {})

    def measured_files(self):
        """A list of all files that had been measured."""
        return list(self.lines.keys())

    def executed_lines(self, filename):
        """A map containing all the line numbers executed in `filename`.

        If `filename` hasn't been collected at all (because it wasn't executed)
        then return an empty map.

        """
        return self.lines.get(filename) or {}

    def executed_arcs(self, filename):
        """A map containing all the arcs executed in `filename`."""
        return self.arcs.get(filename) or {}

    def executed_calling_tests(self, filename):
        """A map containing all the tests callers executed in `filename`."""
        return self.callers.get(filename) or {}

    def add_to_hash(self, filename, hasher):
        """Contribute `filename`'s data to the Md5Hash `hasher`."""
        hasher.update(self.executed_lines(filename))
        hasher.update(self.executed_arcs(filename))
        hasher.update(self.executed_calling_tests(filename))

    def summary(self, fullpath=False):
        """Return a dict summarizing the coverage data.

        Keys are based on the filenames, and values are the number of executed
        lines.  If `fullpath` is true, then the keys are the full pathnames of
        the files, otherwise they are the basenames of the files.

        """
        summ = {}
        if fullpath:
            filename_fn = lambda f: f
        else:
            filename_fn = os.path.basename
        for filename, lines in iitems(self.lines):
            summ[filename_fn(filename)] = len(lines)
        return summ

    def has_arcs(self):
        """Does this data have arcs?"""
        return bool(self.arcs)

    def has_calling_tests(self):
        """Does this data have calling tests data?"""
        return bool(self.callers)


if __name__ == '__main__':
    # Ad-hoc: show the raw data in a data file.
    import pprint, sys
    covdata = CoverageData()
    if sys.argv[1:]:
        fname = sys.argv[1]
    else:
        fname = covdata.filename
    pprint.pprint(covdata.raw_data(fname))
