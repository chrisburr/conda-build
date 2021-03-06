from collections import Hashable
import hashlib
import json
import os
from os.path import join
import sys
import threading

from six import string_types

# TODO :: Remove all use of pyldd
# Currently we verify the output of each against the other
from .pyldd import inspect_linkages as inspect_linkages_pyldd
# lief cannot handle files it doesn't know about gracefully
from .pyldd import codefile_type as codefile_type_pyldd
codefile_type = codefile_type_pyldd
have_lief = False
try:
    import lief
    have_lief = True
except:
    pass


def is_string(s):
    try:
        return isinstance(s, basestring)
    except NameError:
        return isinstance(s, str)


# Some functions can operate on either file names
# or an already loaded binary. Generally speaking
# these are to be avoided, or if not avoided they
# should be passed a binary when possible as that
# will prevent having to parse it multiple times.
def ensure_binary(file):
    if not is_string(file):
        return file
    else:
        try:
            if not os.path.exists(file):
                return []
            return lief.parse(file)
        except:
            print('WARNING: liefldd: failed to ensure_binary({})'.format(file))
    return None


def nm(filename):
    """ Return symbols from *filename* binary """
    done = False
    try:
        binary = lief.parse(filename)  # Build an abstract binary
        symbols = binary.symbols

        if len(symbols) > 0:
            for symbol in symbols:
                print(dir(symbol))
                print(symbol)
                done = True
    except:
        pass
    if not done:
        print("No symbols found")


def codefile_type_liefldd(file, skip_symlinks=True):
    binary = ensure_binary(file)
    result = None
    if binary:
        if binary.format == lief.EXE_FORMATS.PE:
            if lief.PE.DLL_CHARACTERISTICS:
                if binary.header.characteristics & lief.PE.HEADER_CHARACTERISTICS.DLL:
                    result = 'DLLfile'
                else:
                    result = 'EXEfile'
        elif binary.format == lief.EXE_FORMATS.MACHO:
            result = 'machofile'
        elif binary.format == lief.EXE_FORMATS.ELF:
            result = 'elffile'
    return result


if have_lief:
    codefile_type = codefile_type_liefldd


def _trim_sysroot(sysroot):
    while sysroot.endswith('/') or sysroot.endswith('\\'):
        sysroot = sysroot[:-1]
    return sysroot


def get_libraries(file):
    result = []
    binary = ensure_binary(file)
    if binary:
        if binary.format == lief.EXE_FORMATS.PE:
            result = binary.libraries
        else:
            # LIEF returns LC_ID_DYLIB name @rpath/libbz2.dylib in binary.libraries. Strip that.
            binary_name = None
            if binary.format == lief.EXE_FORMATS.MACHO and binary.has_rpath:
                binary_name = [command.name for command in binary.commands
                               if command.command == lief.MachO.LOAD_COMMAND_TYPES.ID_DYLIB]
                binary_name = binary_name[0] if len(binary_name) else None
            result = [l if isinstance(l, string_types) else l.name for l in binary.libraries]
            if binary.format == lief.EXE_FORMATS.MACHO:
                result = [from_os_varnames(binary, l) for l in result
                          if not (binary_name and l.endswith(binary_name))]
    return result


def get_rpaths(file, exe_dirname, envroot, windows_root=''):
    binary = ensure_binary(file)
    rpaths = []
    if binary:
        if binary.format == lief.EXE_FORMATS.PE:
            if not exe_dirname:
                # log = get_logger(__name__)
                # log.warn("Windows library file at %s could not be inspected for rpath.  If it's a standalone "
                #          "thing from a 3rd party, this is safe to ignore." % file)
                return []
            # To allow the unix-y rpath code to work we consider
            # exes as having rpaths of env + CONDA_WINDOWS_PATHS
            # and consider DLLs as having no rpaths.
            # .. scratch that, we don't pass exes in as the root
            # entries so we just need rpaths for all files and
            # not to apply them transitively.
            # https://docs.microsoft.com/en-us/windows/desktop/dlls/dynamic-link-library-search-order
            rpaths.append(exe_dirname.replace('\\', '/'))
            if windows_root:
                rpaths.append('/'.join((windows_root, "System32")))
                rpaths.append(windows_root)
            if envroot:
                # and not lief.PE.HEADER_CHARACTERISTICS.DLL in binary.header.characteristics_list:
                rpaths.extend(list(_get_path_dirs(envroot)))
# This only returns the first entry.
#        elif binary.format == lief.EXE_FORMATS.MACHO and binary.has_rpath:
#            rpaths = binary.rpath.path.split(':')
        elif binary.format == lief.EXE_FORMATS.MACHO and binary.has_rpath:
            rpaths.extend([command.path.rstrip('/') for command in binary.commands
                      if command.command == lief.MachO.LOAD_COMMAND_TYPES.RPATH])
        elif binary.format == lief.EXE_FORMATS.ELF:
            if binary.type == lief.ELF.ELF_CLASS.CLASS32 or binary.type == lief.ELF.ELF_CLASS.CLASS64:
                dynamic_entries = binary.dynamic_entries
                # runpath takes precedence over rpath on GNU/Linux.
                rpaths_colons = [e.runpath for e in dynamic_entries if e.tag == lief.ELF.DYNAMIC_TAGS.RUNPATH]
                if not len(rpaths_colons):
                    rpaths_colons = [e.rpath for e in dynamic_entries if e.tag == lief.ELF.DYNAMIC_TAGS.RPATH]
                for rpaths_colon in rpaths_colons:
                    rpaths.extend(rpaths_colon.split(':'))
    return [from_os_varnames(binary, rpath) for rpath in rpaths]


def get_runpaths(file):
    binary = ensure_binary(file)
    rpaths = []
    if binary:
        if (binary.format == lief.EXE_FORMATS.ELF and  # noqa
            (binary.type == lief.ELF.ELF_CLASS.CLASS32 or binary.type == lief.ELF.ELF_CLASS.CLASS64)):
            dynamic_entries = binary.dynamic_entries
            rpaths_colons = [e.runpath for e in dynamic_entries if e.tag == lief.ELF.DYNAMIC_TAGS.RUNPATH]
            for rpaths_colon in rpaths_colons:
                rpaths.extend(rpaths_colon.split(':'))
    return [from_os_varnames(binary, rpath) for rpath in rpaths]


# TODO :: Consider memoizing instead of repeatedly scanning
# TODO :: libc.so/libSystem.dylib when inspect_linkages(recurse=True)
def _inspect_linkages_this(filename, sysroot='', arch='native'):
    '''

    :param filename:
    :param sysroot:
    :param arch:
    :return:
    '''

    if not os.path.exists(filename):
        return None, [], []
    sysroot = _trim_sysroot(sysroot)
    try:
        binary = lief.parse(filename)
        # Future lief has this:
        # json_data = json.loads(lief.to_json_from_abstract(binary))
        json_data = json.loads(lief.to_json(binary))
        if json_data:
            return filename, json_data['imported_libraries'], json_data['imported_libraries']
    except:
        print('WARNING: liefldd: failed _inspect_linkages_this({})'.format(filename))

    return None, [], []


def to_os_varnames(binary, input_):
    """Don't make these functions - they are methods to match the API for elffiles."""
    if binary.format == lief.EXE_FORMATS.MACHO:
        return input_.replace('$SELFDIR', '@loader_path')     \
                     .replace('$EXEDIR', '@executable_path')  \
                     .replace('$RPATH', '@rpath')
    elif binary.format == lief.EXE_FORMATS.ELF:
        if binary.ehdr.sz_ptr == 8:
            libdir = '/lib64'
        else:
            libdir = '/lib'
        return input.replace('$SELFDIR', '$ORIGIN') \
            .replace(libdir, '$LIB')


def from_os_varnames(binary, input_):
    """Don't make these functions - they are methods to match the API for elffiles."""
    if binary.format == lief.EXE_FORMATS.MACHO:
        return input_.replace('@loader_path', '$SELFDIR')     \
                     .replace('@executable_path', '$EXEDIR')  \
                     .replace('@rpath', '$RPATH')
    elif binary.format == lief.EXE_FORMATS.ELF:
        if binary.type == lief.ELF.ELF_CLASS.CLASS64:
            libdir = '/lib64'
        else:
            libdir = '/lib'
        return input_.replace('$ORIGIN', '$SELFDIR')  \
            .replace('$LIB', libdir)
    elif binary.format == lief.EXE_FORMATS.PE:
        return input_


# TODO :: Use conda's version of this (or move the constant strings into constants.py
def _get_path_dirs(prefix):
    yield join(prefix, 'Library', 'mingw-w64', 'bin')
    yield join(prefix, 'Library', 'usr', 'bin')
    yield join(prefix, 'Library', 'bin')
    yield join(prefix, 'Scripts')
    yield join(prefix, 'bin')


def get_uniqueness_key(file):
    binary = ensure_binary(file)
    if binary.format == lief.EXE_FORMATS.MACHO:
        return binary.name
    elif (binary.format == lief.EXE_FORMATS.ELF
         and  # noqa
         (binary.type == lief.ELF.ELF_CLASS.CLASS32 or binary.type == lief.ELF.ELF_CLASS.CLASS64)):
        dynamic_entries = binary.dynamic_entries
        result = [e.name for e in dynamic_entries if e.tag == lief.ELF.DYNAMIC_TAGS.SONAME]
        if result:
            return result[0]
        return binary.name
    return binary.name


def _get_resolved_location(codefile,
                           unresolved,
                           exedir,
                           selfdir,
                           rpaths_transitive,
                           LD_LIBRARY_PATH='',
                           default_paths=[],
                           sysroot='',
                           resolved_rpath=None):
    '''
       From `man ld.so`

       When resolving shared object dependencies, the dynamic linker first inspects each dependency
       string to see if it contains a slash (this can occur if a shared object pathname containing
       slashes was specified at link time).  If a slash is found, then the dependency string is
       interpreted as a (relative or absolute) pathname, and the shared object is loaded using that
       pathname.

       If a shared object dependency does not contain a slash, then it is searched for in the
       following order:

       o Using the directories specified in the DT_RPATH dynamic section attribute of the binary
         if present and DT_RUNPATH attribute does not exist.  Use of DT_RPATH is deprecated.

       o Using the environment variable LD_LIBRARY_PATH (unless the executable is being run in
         secure-execution mode; see below).  in which case it is ignored.

       o Using the directories specified in the DT_RUNPATH dynamic section attribute of the
         binary if present. Such directories are searched only to find those objects required
         by DT_NEEDED (direct dependencies) entries and do not apply to those objects' children,
         which must themselves have their own DT_RUNPATH entries. This is unlike DT_RPATH,
         which is applied to searches for all children in the dependency tree.

       o From the cache file /etc/ld.so.cache, which contains a compiled list of candidate
         shared objects previously found in the augmented library path. If, however, the binary
         was linked with the -z nodeflib linker option, shared objects in the default paths are
         skipped. Shared objects installed in hardware capability directories (see below) are
         preferred to other shared objects.

       o In the default path /lib, and then /usr/lib. (On some 64-bit architectures, the default
         paths for 64-bit shared objects are /lib64, and then /usr/lib64.)  If the binary was
         linked with the -z nodeflib linker option, this step is skipped.

       Returns a tuple of resolved location, rpath_used, in_sysroot
    '''
    rpath_result = None
    found = False
    ld_library_paths = [] if not LD_LIBRARY_PATH else LD_LIBRARY_PATH.split(':')
    if unresolved.startswith('$RPATH'):
        these_rpaths = [resolved_rpath] if resolved_rpath else \
                        rpaths_transitive + \
                        ld_library_paths + \
                        [dp.replace('$SYSROOT/', sysroot) for dp in default_paths]
        for rpath in these_rpaths:
            resolved = unresolved.replace('$RPATH', rpath) \
                                 .replace('$SELFDIR', selfdir) \
                                 .replace('$EXEDIR', exedir)
            exists = os.path.exists(resolved)
            exists_sysroot = exists and sysroot and resolved.startswith(sysroot)
            if resolved_rpath or exists or exists_sysroot:
                rpath_result = rpath
                found = True
                break
        if not found:
            # Return the so name so that it can be warned about as missing.
            return unresolved, None, False
    elif any(a in unresolved for a in ('$SELFDIR', '$EXEDIR')):
        resolved = unresolved.replace('$SELFDIR', selfdir) \
                             .replace('$EXEDIR', exedir)
        exists = os.path.exists(resolved)
        exists_sysroot = exists and sysroot and resolved.startswith(sysroot)
    else:
        if unresolved.startswith('/'):
            return unresolved, None, False
        else:
            return os.path.join(selfdir, unresolved), None, False

    return resolved, rpath_result, exists_sysroot


# TODO :: Consider returning a tree structure or a dict when recurse is True?
def inspect_linkages_lief(filename, resolve_filenames=True, recurse=True,
                          sysroot='', envroot='', arch='native'):
    # Already seen is partly about implementing single SONAME
    # rules and its appropriateness on macOS is TBD!
    already_seen = set()
    exedir = os.path.dirname(filename)
    binary = lief.parse(filename)
    todo = [[filename, binary]]

    default_paths = []
    if binary.format == lief.EXE_FORMATS.ELF:
        default_paths = ['$SYSROOT/lib', '$SYSROOT/usr/lib']
        if binary.type == lief.ELF.ELF_CLASS.CLASS64:
            default_paths.extend(['$SYSROOT/lib64', '$SYSROOT/usr/lib64'])
    elif binary.format == lief.EXE_FORMATS.MACHO:
        default_paths = ['$SYSROOT/usr/lib']
    elif binary.format == lief.EXE_FORMATS.PE:
        # We do not include C:\Windows nor C:\Windows\System32 in this list. They are added in
        # get_rpaths() instead since we need to carefully control the order.
        default_paths = ['$SYSROOT/System32/Wbem', '$SYSROOT/System32/WindowsPowerShell/v1.0']
    results = set()
    rpaths_by_binary = dict()
    parents_by_filename = dict({filename: None})
    while todo:
        for element in todo:
            todo.pop(0)
            filename2 = element[0]
            binary = element[1]
            uniqueness_key = get_uniqueness_key(binary)
            if uniqueness_key not in already_seen:
                parent_exe_dirname = None
                if binary.format == lief.EXE_FORMATS.PE:
                    tmp_filename = filename2
                    while tmp_filename:
                        if not parent_exe_dirname and codefile_type(tmp_filename) == 'EXEfile':
                            parent_exe_dirname = os.path.dirname(tmp_filename)
                        tmp_filename = parents_by_filename[tmp_filename]
                else:
                    parent_exe_dirname = exedir
                rpaths_by_binary[filename2] = get_rpaths(binary,
                                                         parent_exe_dirname,
                                                         envroot.replace(os.sep, '/'),
                                                         sysroot)
                tmp_filename = filename2
                rpaths_transitive = []
                if binary.format == lief.EXE_FORMATS.PE:
                    rpaths_transitive = rpaths_by_binary[tmp_filename]
                else:
                    while tmp_filename:
                        rpaths_transitive[:0] = rpaths_by_binary[tmp_filename]
                        tmp_filename = parents_by_filename[tmp_filename]
                libraries = get_libraries(binary)
                if filename2 in libraries:  # Happens on macOS, leading to cycles.
                    libraries.remove(filename2)
                # RPATH is implicit everywhere except macOS, make it explicit to simplify things.
                these_orig = [('$RPATH/' + lib if not lib.startswith('/') and not lib.startswith('$') and  # noqa
                               binary.format != lief.EXE_FORMATS.MACHO else lib)
                              for lib in libraries]
                for orig in these_orig:
                    resolved = _get_resolved_location(binary,
                                                      orig,
                                                      exedir,
                                                      exedir,
                                                      rpaths_transitive=rpaths_transitive,
                                                      default_paths=default_paths,
                                                      sysroot=sysroot)
                    if resolve_filenames:
                        results.add(resolved[0])
                        parents_by_filename[resolved[0]] = filename2
                    else:
                        results.add(orig)
                    if recurse:
                        if os.path.exists(resolved[0]):
                            todo.append([resolved[0], lief.parse(resolved[0])])
                already_seen.add(get_uniqueness_key(binary))
    return results


def get_linkages(filename, resolve_filenames=True, recurse=True,
                 sysroot='', envroot='', arch='native'):
    # When we switch to lief, want to ensure these results do not change.
    # if have_lief:
    result_lief = inspect_linkages_lief(filename, resolve_filenames=resolve_filenames, recurse=recurse,
                                        sysroot=sysroot, envroot=envroot, arch=arch)
    result_pyldd = inspect_linkages_pyldd(filename, resolve_filenames=resolve_filenames, recurse=recurse,
                                          sysroot=sysroot, arch=arch)
    # We do not support Windows yet with pyldd.
    if (set(result_lief) != set(result_pyldd) and
       codefile_type(filename) not in ('DLLfile', 'EXEfile')):
        print("WARNING: Disagreement in get_linkages(filename={}, resolve_filenames={}, recurse={}, sysroot={}, envroot={}, arch={}):\n lief: {}\npyldd: {}\n  (using lief)".
              format(filename, resolve_filenames, recurse, sysroot, envroot, arch, result_lief, result_pyldd))
    return result_lief


def get_imports(file, arch='native'):
    binary = ensure_binary(file)
    return [str(i) for i in binary.imported_functions]


def get_exports(file, arch='native'):
    result = []
    if isinstance(file, str):
        if os.path.exists(file) and file.endswith('.a') or file.endswith('.lib'):
            # Crappy, sorry.
            import subprocess
            # syms = os.system('nm -g {}'.filename)
            # on macOS at least:
            # -PgUj is:
            # P: posix format
            # g: global (exported) only
            # U: not undefined
            # j is name only
            if sys.platform == 'darwin':
                flags = '-PgUj'
            else:
                flags = '-P'
            try:
                out, _ = subprocess.Popen(['nm', flags, file], shell=False,
                                stdout=subprocess.PIPE).communicate()
                results = out.decode('utf-8').splitlines()
                exports = [r.split(' ')[0] for r in results if (' T ') in r]
                result = exports
            except OSError:
                # nm may not be available or have the correct permissions, this
                # should not cause a failure, see gh-3287
                print('WARNING: nm: failed to get_exports({})'.format(file))

    if not result:
        binary = ensure_binary(file)
        if binary:
            result = [str(e) for e in binary.exported_functions]
    return result


def get_relocations(filename, arch='native'):
    if not os.path.exists(filename):
        return []
    try:
        binary = lief.parse(filename)
        res = []
        if len(binary.relocations):
            for r in binary.relocations:
                if r.has_symbol:
                    if r.symbol and r.symbol.name:
                        res.append(r.symbol.name)
            return res
    except:
        print('WARNING: liefldd: failed get_relocations({})'.format(filename))

    return []


def get_symbols(file, defined=True, undefined=True, arch='native'):
    binary = ensure_binary(file)
    try:
        if binary.__class__ == lief.MachO.Binary and binary.has_dyld_info:
            dyscmd = binary.dynamic_symbol_command
            first_undefined_symbol = dyscmd.idx_undefined_symbol
            last_undefined_symbol = first_undefined_symbol + dyscmd.nb_undefined_symbols - 1
        else:
            first_undefined_symbol = 0
            last_undefined_symbol = -1
        res = []
        if len(binary.exported_functions):
            syms = binary.exported_functions
        elif len(binary.symbols):
            syms = binary.symbols
        elif len(binary.static_symbols):
            syms = binary.static_symbols
        for index, s in enumerate(syms):
            is_undefined = index >= first_undefined_symbol and index <= last_undefined_symbol
            if binary.__class__ != lief.MachO.Binary:
                if isinstance(s, str):
                    res.append(s)
                else:
                    if s.exported and s.imported:
                        print("Weird, symbol {} is both imported and exported".format(s.name))
                    if s.exported:
                        is_undefined = True
                    elif s.imported:
                        is_undefined = False

                    if is_undefined and undefined:
                        res.append(s.name)
                    elif not is_undefined and defined:
                        res.append(s.name)
            # else:
            #     print("Skipping {}, is_undefined {}, defined {}, undefined {}".format(s.name, is_undefined, defined, undefined))
        return res
    except:
        print('WARNING: liefldd: failed get_symbols({})'.format(file))

    return []


class memoized_by_arg0_inode(object):
    """Decorator. Caches a function's return value each time it is called.
    If called later with the same arguments, the cached value is returned
    (not reevaluated).

    The first argument is required to be an existing filename and it is
    always converted to an inode number.
    """
    def __init__(self, func):
        self.func = func
        self.cache = {}
        self.lock = threading.Lock()

    def __call__(self, *args, **kw):
        newargs = []
        for arg in args:
            if arg is args[0]:
                s = os.stat(arg)
                arg = s.st_ino
            if isinstance(arg, list):
                newargs.append(tuple(arg))
            elif not isinstance(arg, Hashable):
                # uncacheable. a list, for instance.
                # better to not cache than blow up.
                return self.func(*args, **kw)
            else:
                newargs.append(arg)
        newargs = tuple(newargs)
        key = (newargs, frozenset(sorted(kw.items())))
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            else:
                value = self.func(*args, **kw)
                self.cache[key] = value
                return value


class memoized_by_arg0_filehash(object):
    """Decorator. Caches a function's return value each time it is called.
    If called later with the same arguments, the cached value is returned
    (not reevaluated).

    The first argument is required to be an existing filename and it is
    always converted to an inode number.
    """
    def __init__(self, func):
        self.func = func
        self.cache = {}
        self.lock = threading.Lock()

    def __call__(self, *args, **kw):
        newargs = []
        for arg in args:
            if arg is args[0]:
                sha1 = hashlib.sha1()
                with open(arg, 'rb') as f:
                    while True:
                        data = f.read(65536)
                        if not data:
                            break
                        sha1.update(data)
                arg = sha1.hexdigest()
            if isinstance(arg, list):
                newargs.append(tuple(arg))
            elif not isinstance(arg, Hashable):
                # uncacheable. a list, for instance.
                # better to not cache than blow up.
                return self.func(*args, **kw)
            else:
                newargs.append(arg)
        newargs = tuple(newargs)
        key = (newargs, frozenset(sorted(kw.items())))
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            else:
                value = self.func(*args, **kw)
                self.cache[key] = value
                return value


@memoized_by_arg0_inode
def get_exports_memoized(filename, arch='native'):
    return get_exports(filename, arch=arch)


@memoized_by_arg0_filehash
def get_imports_memoized(filename, arch='native'):
    return get_imports(filename, arch=arch)


@memoized_by_arg0_filehash
def get_relocations_memoized(filename, arch='native'):
    return get_relocations(filename, arch=arch)


@memoized_by_arg0_filehash
def get_symbols_memoized(filename, defined, undefined, arch):
    return get_symbols(filename, defined=defined, undefined=undefined, arch=arch)


@memoized_by_arg0_filehash
def get_linkages_memoized(filename, resolve_filenames, recurse,
                          sysroot='', envroot='', arch='native'):
    return get_linkages(filename, resolve_filenames=resolve_filenames,
                        recurse=recurse, sysroot=sysroot, envroot=envroot, arch=arch)
