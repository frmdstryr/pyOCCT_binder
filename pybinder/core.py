# This file is part of pyOCCT_binder which automatically generates Python
# bindings to the OpenCASCADE geometry kernel using pybind11.
#
# Copyright (C) 2016-2018  Laughlin Research, LLC
# Copyright (C) 2019 Trevor Laughlin
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
import os
import re
import sys
import io
import hashlib
import warnings
from collections import OrderedDict
from ctypes import c_uint
from fnmatch import fnmatch
from functools import cached_property
from glob import glob

from clang.cindex import (AccessSpecifier, Index, TranslationUnit,
                          CursorKind, TypeKind, Cursor, LibclangError, conf)

try:
    conf.get_cindex_library()
except LibclangError:
    if sys.platform == 'win32' and (conda_prefix := os.environ.get("CONDA_PREFIX")):
        if libclang_dlls := glob(f"{conda_prefix}/Library/bin/libclang*.dll"):
            conf.set_library_file(libclang_dlls[0])


from pybinder import clangext
from pybinder.common import SRC_PREFIX, PY_OPERATORS


# Patches for libclang
clangext.monkeypatch_cursor('get_specialization',
                            'clang_getSpecializedCursorTemplate',
                            [Cursor], Cursor)

clangext.monkeypatch_cursor('get_template_kind',
                            'clang_getTemplateCursorKind',
                            [Cursor], c_uint)

clangext.monkeypatch_cursor('get_num_overloaded_decl',
                            'clang_getNumOverloadedDecls',
                            [Cursor], c_uint)

clangext.monkeypatch_cursor('get_overloaded_decl',
                            'clang_getOverloadedDecl',
                            [Cursor, c_uint], Cursor)

logger = open('log.txt', 'w')


def overwrite_if_changed(path: str, source: io.StringIO):
    """
    Only overwrite the file at path if the buffer changed
    """
    data = source.getvalue().encode()
    if os.path.exists(path):
        new_hash = hashlib.sha256(data).hexdigest()
        with open(path, 'rb') as f:
            old_hash = hashlib.sha256(f.read()).hexdigest()
            if new_hash == old_hash:
                 logger.write(f"Skipping {path}, contents unchanged")
                 return
    with open(path, 'wb') as f:
        f.write(data)


class MacroForHandle(object):
    """
    Special class for handling of certain macros
    """
    relevant_macros = {'DEFINE_HARRAY1',
                       'DEFINE_HARRAY2',
                       'DEFINE_HSEQUENCE'}

    macro2func = {'DEFINE_HARRAY1': 'bind_Define_HArray1',
                  'DEFINE_HARRAY2': 'bind_Define_HArray2',
                  'DEFINE_HSEQUENCE': 'bind_Define_HSequence'}

    macro2headers = {'DEFINE_HARRAY1': ['bind_Define_HArray1.hxx'],
                     'DEFINE_HARRAY2': ['bind_Define_HArray2.hxx'],
                     'DEFINE_HSEQUENCE': ['bind_Define_HSequence.hxx']}

    def __init__(self, macro, type1, type2):
        self.name = macro
        self.type1 = type1
        self.type2 = type2

    def generate(self):
        """
        Generate the binding text for a special case macro.
        """
        name = self.macro2func[self.name]
        return '{}<{}, {}>(mod, \"{}\");'.format(name, self.type1, self.type2,
                                                 self.type1)

    def headers(self):
        """
        Return list of extra headers for binding these special macros.
        """
        return self.macro2headers[self.name]


class Generator(object):
    """
    Main class for OCCT header parsing and binding generation.
    :ivar str name: Name of the main package.
    """

    available_mods = set()
    available_incs = set()
    available_templates = set()
    excluded_classes = set()
    excluded_functions = set()
    excluded_rtypes = set()
    excluded_enums = set()
    excluded_fnames = set()
    excluded_mods = set()
    excluded_typedefs = set()
    excluded_fields = set()
    excluded_headers = set()
    excluded_namespaces: set[str] = {"std"}
    nodelete = set()
    nested_classes = set()
    downcast_classes = set()
    skipped = set()
    immutable = set()
    split = set()

    # Convert to `void *`
    opaque_types = set()

    # Mapping of package name to module
    namespace = dict()

    # Includes added to all modules
    common_includes = set()

    excluded_bases = dict()
    import_guards = dict()
    plus_headers = dict()
    minus_headers = dict()
    python_names = dict()
    excluded_imports = dict()
    call_guards = dict()
    keep_alive = dict()
    before_type = dict()
    after_type = dict()
    patches = dict()
    return_policies = dict()
    before_module = dict()
    to_bind: set[CursorKind]

    sort_order = dict() # type: dict[str, list[str, int]]

    _mods = OrderedDict()

    def __init__(self, package_name, namespace, all_includes, include_dirs=None):
        self.package_name = package_name
        self._indx = Index.create()

        # If all_includes is a path just use that
        if isinstance(all_includes, str):
            if include_dirs is None:
                include_dirs = [all_includes]
            all_includes = os.listdir(all_includes)

        # Primary include directories
        self._main_includes = list(include_dirs or [])

        # Include directories
        self.include_dirs = []

        # Compiler arguments
        self.compiler_args = {}

        # Translation unit and main cursor
        self._tu = None
        self._tu_binder = None

        # Build available include files
        Generator.namespace = namespace
        Generator.common_includes = set([f'py{package_name}_Common.hxx'])
        Generator.available_incs = frozenset(all_includes)
        Generator.available_mods = frozenset(namespace[package_name])

        # Turn on/off binding of certain declarations for debugging
        self.bind_enums = True
        self.bind_functions = True
        self.bind_classes = True
        self.bind_typedefs = True
        self.bind_class_templates = True


    @property
    def tu(self):
        """
        :return: The translation unit.
        :rtype: clang.cindex.TranslationUnit
        """
        return self._tu

    @property
    def tu_binder(self):
        """
        :return: The translation unit binder.
        :rtype: binder.core.CursorBinder
        """
        return self._tu_binder

    @property
    def modules(self):
        """
        :return: The modules.
        :rtype: list(binder.core.Module)
        """
        return list(self._mods.values())

    def process_config(self, fn):
        """
        Process a configuration file.
        :param str fn: The file.
        :return: None.
        """
        logger.write('Processing configuration file: {}.\n'.format(fn))
        with open(fn, 'r') as f:
            for line_number, line in enumerate(f):
                try:
                    line = line.strip()

                    # Comment or blank line
                    if line.startswith('#') or not line:
                        continue

                    # Compiler argument
                    if line.startswith('+arg'):
                        line = line.replace('+arg', '')
                        line = line.strip()
                        platform, arg = line.split(':')
                        platform = platform.strip()
                        arg = arg.strip()
                        if platform in self.compiler_args:
                            self.compiler_args[platform].append(arg)
                        else:
                            self.compiler_args[platform] = [arg]
                        continue

                    # Include directory
                    if line.startswith('+include'):
                        line = line.replace('+include', '')
                        line = line.strip()
                        self.include_dirs.append(line)
                        continue

                    # Sort order
                    if line.startswith('+sort'):
                        line = line.replace('+sort', '').strip()
                        mod, value = line.split(':')
                        mod = mod.strip()
                        pattern, prio = value.split("=")
                        if mod not in Generator.sort_order:
                            Generator.sort_order[mod] = []
                        Generator.sort_order[mod].append(
                            (pattern.strip(), int(prio.strip()))
                        )
                        continue

                    # Excluded header
                    if line.startswith('-header*'):
                        line = line.replace('-header*', '')
                        line = line.strip()
                        self.excluded_headers.add(line)
                        continue

                    # Excluded classes
                    if line.startswith('-class'):
                        action, line = line.split(" ", 1)
                        if "@" in action:
                            # Platform specific
                            action, platform = action.split("@")
                            if platform != sys.platform:
                                continue
                        line = line.strip()
                        self.excluded_classes.add(line)
                        continue

                    # Excluded typedefs
                    if line.startswith('-typedef'):
                        line = line.replace('-typedef', '')
                        line = line.strip()
                        self.excluded_typedefs.add(line)
                        continue

                    # Excluded functions
                    if line.startswith('-function*'):
                        line = line.replace('-function*', '')
                        line = line.strip().replace("[", "[[]")
                        self.excluded_fnames.add(line)
                        continue

                    if line.startswith('-function'):
                        action, line = line.split(" ", 1)
                        if "@" in action:
                            # Platform specific
                            action, platform = action.split("@")
                            if platform != sys.platform:
                                continue
                        line = line.strip().replace("[", "[[]")
                        self.excluded_functions.add(line)
                        continue

                    if line.startswith('-rtype'):
                        line = line.replace('-rtype', '')
                        line = line.strip().replace("[", "[[]")
                        self.excluded_rtypes.add(line)
                        continue

                    # Excluded enums
                    if line.startswith('-enum'):
                        line = line.replace('-enum', '')
                        line = line.strip()
                        self.excluded_enums.add(line)
                        continue

                    # Excluded modules
                    if line.startswith('-module'):
                        line = line.replace('-module', '')
                        line = line.strip()
                        self.excluded_mods.add(line)
                        continue

                    # Import guards
                    if line.startswith('+iguard'):
                        line = line.replace('+iguard', '')
                        line = line.strip()
                        mod, other = line.split(':')
                        mod = mod.strip()
                        other = other.strip()
                        if mod in self.import_guards:
                            self.import_guards[mod].add(other)
                        else:
                            self.import_guards[mod] = {other}
                        continue

                    # Plus headers
                    if line.startswith('+header'):
                        line = line.replace('+header', '')
                        line = line.strip()
                        mod, header = line.split(':')
                        mod = mod.strip()
                        header = header.strip()
                        if mod in self.plus_headers:
                            self.plus_headers[mod].append(header)
                        else:
                            self.plus_headers[mod] = [header]
                        continue

                    # Minus headers
                    if line.startswith('-header'):
                        line = line.replace('-header', '')
                        line = line.strip()
                        mod, header = line.split(':')
                        mod = mod.strip()
                        header = header.strip()
                        if mod in self.minus_headers:
                            self.minus_headers[mod].append(header)
                        else:
                            self.minus_headers[mod] = [header]
                        continue

                    # Python names
                    if line.startswith('+pname'):
                        line = line.replace('+pname', '')
                        line = line.strip()
                        type_, name = line.split('-->', 1)
                        type_ = type_.strip()
                        name = name.strip()
                        self.python_names[type_] = name
                        continue

                    # nodelete
                    if line.startswith('+nodelete'):
                        line = line.replace('+nodelete', '')
                        line = line.strip()
                        self.nodelete.add(line)
                        continue

                    # Excluded bases
                    if line.startswith('-base'):
                        line = line.replace('-base', '')
                        line = line.strip()
                        qname, base = line.split(':', 1)
                        qname = qname.strip()
                        base = base.strip()
                        if qname in self.excluded_bases:
                            self.excluded_bases[qname].append(base)
                        else:
                            self.excluded_bases[qname] = [base]
                        continue

                    # Excluded fields
                    if line.startswith('-field'):
                        line = line.replace('-field', '')
                        line = line.strip()
                        self.excluded_fields.add(line)
                        continue

                    # Excluded imports
                    if line.startswith('-import'):
                        line = line.replace('-import', '')
                        line = line.strip()
                        mod1, mod2 = line.split(':', 1)
                        mod1 = mod1.strip()
                        mod2 = mod2.strip()
                        if mod1 in self.excluded_imports:
                            self.excluded_imports[mod1].append(mod2)
                        else:
                            self.excluded_imports[mod1] = [mod2]
                        continue

                    # Return policies
                    if line.startswith('+return_policy'):
                        line = line.replace('+return_policy', '').strip()
                        qname, policy = map(str.strip, line.split('-->', 1))
                        self.return_policies[qname] = policy

                    # Call guards
                    if line.startswith('+cguard'):
                        line = line.replace('+cguard', '')
                        line = line.strip()
                        qname, mod = line.split('-->', 1)
                        qname = qname.strip()
                        mod = mod.strip()
                        txt = 'py::call_guard<Import{}>()'.format(mod)
                        if qname in self.call_guards:
                            self.call_guards[qname].append(txt)
                        else:
                            self.call_guards[qname] = [txt]
                        continue

                    # Keep alive
                    if line.startswith('+keep_alive'):
                        line = line.replace('+keep_alive', '')
                        line = line.strip()
                        qname, params = line.split('-->', 1)
                        self.keep_alive[qname.strip()] = params.strip()
                        continue

                    # Nested classes
                    if line.startswith('+nested'):
                        line = line.replace('+nested', '')
                        line = line.strip()
                        self.nested_classes.add(line)
                        continue

                    if line.startswith('+downcast'):
                        line = line.replace('+downcast', '')
                        line = line.strip()
                        self.downcast_classes.add(line)
                        continue

                    # Skipped binders
                    if line.startswith('+skip'):
                        line = line.replace('+skip', '')
                        line = line.strip()
                        self.skipped.add(line)
                        continue

                    # Manual text before a type
                    if line.startswith('+before_type'):
                        line = line.replace('+before_type', '', 1)
                        line = line.strip()
                        qname, txt = line.split('-->', 1)
                        qname = qname.strip()
                        txt = txt.strip()
                        if qname in self.before_type:
                            self.before_type[qname].append(txt)
                        else:
                            self.before_type[qname] = [txt]
                        continue

                    # Extra text after a type
                    if line.startswith('+after_type'):
                        line = line.replace('+after_type', '', 1)
                        line = line.strip()
                        qname, txt = line.split('-->', 1)
                        qname = qname.strip()
                        txt = txt.strip()
                        if qname in self.after_type:
                            self.after_type[qname].append(txt)
                        else:
                            self.after_type[qname] = [txt]
                        continue

                    # Immutable types
                    if line.startswith('+immutable'):
                        line = line.replace('+immutable', '')
                        line = line.strip()
                        self.immutable.add(line)
                        continue

                    # Split modules
                    if line.startswith('+split'):
                        line = line.replace('+split', '')
                        line = line.strip()
                        self.split.add(line)
                        continue

                    # Replace text in file
                    if line.startswith('+patch'):
                        line = line.replace('+patch', '')
                        line = line.strip()
                        qname, txt = line.split(':', 1)
                        qname = qname.strip()
                        pair = txt.strip().split('-->', 1)
                        if qname in self.patches:
                            self.patches[qname].append(pair)
                        else:
                            self.patches[qname] = [pair]
                        continue

                    if line.startswith('+opaque'):
                        line = line[len("+opaque"):].strip()
                        self.opaque_types.add(line)
                        continue

                    # Manual text before a module
                    if line.startswith('+before_module'):
                        line = line.replace('+before_module', '', 1)
                        line = line.strip()
                        mod, txt = line.split('-->', 1)
                        mod = mod.strip()
                        txt = txt.strip()
                        if mod in self.before_module:
                            self.before_module[mod].append(txt)
                        else:
                            self.before_module[mod] = [txt]
                        continue
                except Exception as e:
                    i = line_number + 1
                    raise RuntimeError(f"Error in config at line {i}: {e}")

    def parse(self, file_):
        """
        Parse the main include file.
        :param str file_: The main include file to parse.
        :return: None
        """
        logger.write('Parsing headers...\n')

        args = []
        # Any
        if 'any' in self.compiler_args:
            for arg in self.compiler_args['any']:
                args.append(arg)
                logger.write('\tCompiler argument: {}\n'.format(arg))

        # win32
        if sys.platform == 'win32' and 'win32' in self.compiler_args:
            for arg in self.compiler_args['win32']:
                args.append(arg)
                logger.write('\tCompiler argument: {}\n'.format(arg))

        # linux
        if sys.platform == 'linux' and 'linux' in self.compiler_args:
            for arg in self.compiler_args['linux']:
                args.append(arg)
                logger.write('\tCompiler argument: {}\n'.format(arg))

        # osx
        if sys.platform == 'darwin' and 'osx' in self.compiler_args:
            for arg in self.compiler_args['osx']:
                args.append(arg)
                logger.write('\tCompiler argument: {}\n'.format(arg))

        for path in self.include_dirs + self._main_includes:
            args += [''.join(['-I', path])]
            logger.write('\tInclude path: {}\n'.format(path))

        self._tu = self._indx.parse(file_, args,
                                    options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        logger.write('done.\n\n')

        self._tu_binder = CursorBinder(self.tu.cursor)

    def dump_diagnostics(self, severity=4):
        """
        Dump diagnostic information.

        :param int severity: The level at or above to dump diagnostics. Anything lower than this
            will be not be printed.

        :return: None.
        """
        print('----------------------')
        print('DIAGNOSTIC INFORMATION')
        print('----------------------')
        other_issues = 0
        for diag in self.tu.diagnostics:
            if diag.severity < severity:
                other_issues += 1
                continue
            print('---')
            print('SEVERITY: {}'.format(diag.severity))
            print('LOCATION: {}'.format(diag.location))
            print('MESSAGE: {}'.format(diag.spelling))
            print('---')

        msg = 'Complete with {} issues with lower than {} severity not shown.'.format(other_issues,
                                                                                      severity)
        print(msg)
        print('----------------------')

    def save(self, fname):
        """
        Save the TranslationUnit to a file.
        :param str fname: The filename.
        :return: None.
        """
        self.tu.save(fname)

    def load(self, fname):
        """
        Load a TranslationUnit from a saved AST file.
        :param str fname: The filename.
        :return: None.
        """
        self._tu = TranslationUnit.from_ast_file(fname, self._indx)

    def traverse(self):
        """
        Traverse parsed headers and gather binders.
        :return: None.
        """
        available_macros = {}
        # First gather all the handle macros to handle them specially
        for binder in self.tu_binder.get_children_of_kind(
                CursorKind.MACRO_INSTANTIATION):
            if binder.spelling.upper() in MacroForHandle.relevant_macros:
                tokens = list(binder.cursor.get_tokens())
                macro = tokens[0].spelling
                txt = ''.join([t.spelling for t in tokens])
                type1, type2 = re.findall(r'\((.*)\)', txt)[0].split(',')
                macro = MacroForHandle(macro, type1, type2)
                available_macros[type1] = macro

        # What to bind
        to_bind = set()
        if self.bind_enums:
            to_bind.add(CursorKind.ENUM_DECL)
        if self.bind_functions:
            to_bind.add(CursorKind.FUNCTION_DECL)
        if self.bind_classes:
            to_bind.add(CursorKind.STRUCT_DECL)
            to_bind.add(CursorKind.CLASS_DECL)
        if self.bind_typedefs:
            to_bind.add(CursorKind.TYPEDEF_DECL)
        if self.bind_class_templates:
            to_bind.add(CursorKind.CLASS_TEMPLATE)
        self.to_bind = to_bind

        enum_log = []
        function_log = []
        types_log = []
        template_log = []
        unknown_log = []

        # Build aliases based on canonical types
        canonical_types = {}
        alias_log = []

        logs = {
            "alias": alias_log,
            "enum": enum_log,
            "function": function_log,
            "types": types_log,
            "template": template_log,
            "unknown": unknown_log,
        }

        logger.write('Traversing...\n')
        # Traverse the translation unit and group the binders into modules
        for binder in self.tu_binder.get_children():
            self.traverse_binder(binder, logs, canonical_types, available_macros)
        logger.write('done.\n\n')

        logger.write('Enums...\n')
        for txt in enum_log:
            logger.write(txt)
        logger.write('done.\n\n')

        logger.write('Functions...\n')
        for txt in function_log:
            logger.write(txt)
        logger.write('done.\n\n')

        logger.write('Types...\n')
        for txt in types_log:
            logger.write(txt)
        logger.write('done.\n\n')

        logger.write('Class templates...\n')
        for txt in template_log:
            logger.write(txt)
        logger.write('done.\n\n')

        logger.write('Aliases...\n')
        for txt in alias_log:
            logger.write(txt)
        logger.write('done.\n\n')

        logger.write('Unknowns...\n')
        for txt in unknown_log:
            logger.write(txt)
        logger.write('done.\n\n')

    def traverse_binder(
        self,
        binder: "CursorBinder",
        logs: dict[str, list[str]],
        canonical_types: dict,
        available_macros: dict,
    ):
        # Only bind definitions
        # TODO Why is IGESFile and StepFile not considered definitions?
        if (not binder.is_definition and
                not binder.spelling.startswith('IGESFile') and
                not binder.spelling.startswith('StepFile')):
            return

        if binder.is_namespace:
            # clang 20+ must manually walk ns
            if binder.spelling.startswith("__") or binder.spelling in self.excluded_namespaces:
                return
            for binder in binder.get_children():
                self.traverse_binder(binder, logs, canonical_types, available_macros)
            return

        # Bind only these types of cursors
        if binder.kind not in self.to_bind:
            return

        # Skip if it's a "Handle_*" definition
        if binder.spelling.startswith('Handle_'):
            return

        # Skip if function starts with "operator"
        if binder.is_function and binder.spelling.startswith('operator'):
            return

        # Add binder only if it's in an OCCT header file.
        inc = binder.filename
        if inc in self.excluded_headers or inc not in self.available_incs:
            return

        # Add binder if it's in an available module
        mod_name = binder.module_name
        if mod_name not in Generator.available_mods:
            return

        # Add to module
        mod = self.get_module(mod_name)
        if not mod:
            return

        qname = binder.qualified_name

        # Skip if specified
        if qname in self.skipped:
            msg = '\tSkipping {}.\n'.format(binder.type.spelling)
            logger.write(msg)
            return

        # Check for anon/untagged enum
        if not qname and binder.is_enum:
            qname = binder.type.spelling

        if not qname:
            msg = '\tNo qualified name. Skipping {}.\n'.format(
                binder.type.spelling
            )
            logger.write(msg)
            return

        if binder.is_enum:
            mod.enums.append(binder)
            msg = '\tFound enum: {}\n'.format(qname)
            logs["enum"].append(msg)
        elif binder.is_function:
            mod.funcs.append(binder)
            msg = '\tFound function: {}({})\n'.format(qname, mod_name)
            logs["function"].append(msg)
        elif binder.is_class:
            mod.types.append(binder)
            msg = '\tFound class: {}\n'.format(qname)
            logs["types"].append(msg)
            spelling = binder.type.get_canonical().spelling
            canonical_types[spelling] = binder
            # Check for macro
            if qname in available_macros:
                binder.macro = available_macros[qname]
                msg = '\tFound macro: {}-->{}\n'.format(qname,
                                                        binder.macro.name)
                logs["types"].append(msg)
        elif binder.is_typedef:
            mod.types.append(binder)
            msg = '\tFound typedef: {}\n'.format(qname)
            logs["types"].append(msg)
            # Check for an alias
            spelling = binder.type.get_canonical().spelling
            if spelling in canonical_types:
                alias = canonical_types[spelling]
                binder.alias = alias
                alias_qname = alias.qualified_name
                msg = '\tFound alias: {}-->{}\n'.format(qname,
                                                        alias_qname)
                logs["alias"].append(msg)
            else:
                canonical_types[spelling] = binder
        elif binder.is_class_template:
            mod.templates.append(binder)
            msg = '\tFound class template: {}\n'.format(qname)
            logs["template"].append(msg)
        else:
            msg = '\tFound unknown cursor: {}\n'.format(qname)
            logs["unknown"].append(msg)

    def build_includes(self):
        """
        Build include files for the modules.
        :return: None.
        """
        logger.write('Building includes...\n')
        for mod in self.modules:
            mod.build_includes()
        logger.write('done.\n\n')

    def build_imports(self):
        """
        Build module imports.
        :return: None.
        """
        # Import module based on prefix of header files
        for mod in self.modules:
            for inc_file in mod.includes:
                if '_' in inc_file:
                    delim = '_'
                else:
                    delim = '.'
                try:
                    other_name = inc_file.split(delim)[0]
                except (IndexError, AttributeError):
                    continue

                # Check available
                if Generator.get_namespace(other_name) is None:
                    continue

                # Don't add this module
                if mod.name == other_name:
                    continue

                # Check excluded
                if mod.name in Generator.excluded_imports:
                    if other_name in Generator.excluded_imports[mod.name]:
                        continue

                # Add import
                if other_name not in mod.imports:
                    mod.imports.append(other_name)

    def sort_binders(self):
        """
        Sort class binders so they are ordered based on their base
        classes.
        :return: None.
        """
        logger.write('Sorting binders...\n')
        for mod in self.modules:
            mod.sort_binders()
        logger.write('done.\n\n')

    def bind(self, path):
        """
        Bind the library.
        :param str path: Path to write sub-folders.
        :return:
        """
        logger.write('Binding types...\n')
        for mod in self.modules:
            if mod.is_excluded:
                 continue
            mod.bind(path)
        logger.write('done.\n\n')

    def bind_templates(self, path):
        """
        Bind the library.
        :param str path: Path to write sub-folders.
        :return:
        """
        logger.write('Binding templates...\n')
        for mod in self.modules:
            mod.bind_templates(path)
        logger.write('done.\n\n')

    def is_module(self, name):
        """
        Check if the name is an available module.
        :param str name: The name.
        :return: *True* if an available module, *False* otherwise.
        :rtype: bool
        """
        return name in self._mods

    def check_circular(self):
        """
        Check for circular imports.
        :return: None.
        """
        logger.write('Finding circular imports...\n')
        mods = self.modules
        for i, mod1 in enumerate(mods):
            for mod2 in mods[i + 1:-1]:
                if mod1.is_circular(mod2):
                    name1, name2 = mod1.name, mod2.name
                    msg = '\tFound circular import: {} <--> {}\n'.format(name1,
                                                                         name2)
                    logger.write(msg)

    @classmethod
    def get_module(cls, name):
        """
        Get a module by name or return a new one.
        :param str name: Module name.
        :return: The existing module or new one.
        :rtype: binder.core.Module
        """
        if name not in Generator.available_mods:
            return None

        try:
            return cls._mods[name]
        except KeyError:
            mod = Module(name)
            cls._mods[name] = mod
            return mod

    @classmethod
    def get_namespace(cls, name):
        """
        Get the namespace of a module
        :param str name: Module name.
        :return: The module namespace name or None.
        :rtype: str
        """
        for namespace, mods in cls.namespace.items():
            if name in mods:
                return namespace


class Module(object):
    """
    Module class containing binders.
    :param str name: Module name.
    :ivar str name: Module name.
    :ivar list(str) includes: List of relevant include files for this module.
    :ivar list(binder.core.CursorBinder) enums: List of binders around
        enumerations.
    :ivar list(binder.core.CursorBinder) funcs: List of binders around
        functions.
    :ivar list(binder.core.CursorBinder) classes: List of binders around
        classes.
    :ivar list(binder.core.CursorBinder) typedefs: List of binders around
        typedefs.
    :ivar list(binder.core.CursorBinder) templates: List of binders around
        class templates.
    :ivar list(binder.core.Module) imports: List of other modules to import.
    :ivar list(binder.core.CursorBinder) sorted_binders: List of binders after
        sorting.
    """

    def __init__(self, name):
        self.name = name

        self.enums = []
        self.funcs = []
        self.types = []
        self.templates = []

        self.sorted_binders = []

        self.includes = []
        self.imports = []

    def __repr__(self):
        return 'Module: {}'.format(self.name)

    def sort_binders(self):
        """
        Sort class binders so they are ordered based on their base
        classes.
        :return: None.
        """
        # Sort enums based on their file location. Sometimes multiple enums are
        # defined in a single file, so group them together.
        file2enum = {}
        for enum in self.enums:
            if enum.filename in file2enum:
                master = file2enum[enum.filename]
                master.grouped_binders.append(enum)
                enum.skip = True
            else:
                file2enum[enum.filename] = enum
                self.sorted_binders.append(enum)

        # Sort functions by their name. They may be overloaded so group them
        # together.
        spelling2func = {}
        for func in self.funcs:
            if func.spelling in spelling2func:
                master = spelling2func[func.spelling]
                master.grouped_binders.append(func)
                func.skip = True
            else:
                spelling2func[func.spelling] = func
                self.sorted_binders.append(func)

        # Bind types
        self.sorted_binders += self.types
        original_order = {
            v: i
            for i, v in enumerate(self.sorted_binders)
        }

        if sort_order := Generator.sort_order.get(self.name):
            # Hack to workaround TColgp not ordering
            # base classes properly
            def sort_fn(v):
                priority = 10000
                # TODO: Push into config
                for pattern, prio in sort_order:
                    if pattern in v.spelling:
                        priority = prio
                return (priority, original_order[v])


            self.sorted_binders.sort(key=sort_fn)


        # binders1 = list(self.classes)
        # canonical2original = {}
        # # Use canonical for typedefs
        # for binder in self.typedefs:
        #     ut = binder.underlying_typedef_type
        #     binders1.append(ut.get_canonical().get_declaration())
        #     canonical2original[binders1[-1]] = binder
        #
        # binders2 = [b.qualified_name for b in binders1]
        # if not binders2:
        #     return None
        #
        # repeat = True
        # while repeat:
        #     repeat = False
        #
        #     for b in binders1:
        #         if b.qualified_name not in binders2:
        #             continue
        #         i1 = binders2.index(b.qualified_name)
        #         for dep in b.all_bases:
        #             dep = dep.get_definition()
        #             if dep.qualified_name not in binders2:
        #                 continue
        #             i2 = binders2.index(dep.qualified_name)
        #             if i1 < i2:
        #                 binders1[i1], binders1[i2] = (binders1[i2],
        #                                               binders1[i1])
        #                 binders2[i1], binders2[i2] = (binders2[i2],
        #                                               binders2[i1])
        #                 repeat = True
        #
        #                 n1 = binders1[i2].spelling
        #                 n2 = binders1[i1].spelling
        #                 msg = '\tSwapping {}<-->{}.\n'.format(n1, n2)
        #                 logger.write(msg)
        #
        # for binder in binders1:
        #     if binder in canonical2original:
        #         self.sorted_binders.append(canonical2original[binder])
        #     else:
        #         self.sorted_binders.append(binder)

    def build_includes(self):
        """
        Build list of include files for the module.
        :return: None.
        """
        self.includes = list(Generator.common_includes)

        # Excluded headers per module
        minus_headers = set()
        if self.name in Generator.minus_headers:
            minus_headers = set(Generator.minus_headers[self.name])

        # Extra headers per module
        if self.name in Generator.plus_headers:
            for inc in Generator.plus_headers[self.name]:
                if inc not in self.includes:
                    self.includes.append(inc)

        # Headers for binders in module
        all_binders = self.sorted_binders + self.templates
        for binder in all_binders:
            binders = [binder] + binder.grouped_binders
            for binder_ in binders:
                temp = binder_.build_includes()
                # Don't add includes for class templates since they are in a
                # different file
                if binder_.is_class_template:
                    continue
                for f in temp:
                    if (f not in self.includes and
                            f not in minus_headers and
                            f not in Generator.excluded_headers):
                        self.includes.append(f)

    def is_dependent(self, other):
        """
        Check to see if the this module is dependent on the other module based on their imports.
        :param binder.core.Module other: The other module.
        :return: *True* if dependent, *False* otherwise.
        :rtype: bool
        """
        if not self.imports and not other.imports:
            return False

        visited, stack = set(), list(self.imports)
        while stack:
            mod_name = stack.pop(0)
            if mod_name in visited:
                continue
            visited.add(mod_name)
            if mod_name == other.name:
                return True
            mod = Generator.get_module(mod_name)
            if mod is None:
                return False  # External namespace
            stack = list(mod.imports) + stack
        return False

    def is_circular(self, other):
        """
        Check if this module and the other try to import each other.
        :param binder.core.Module other: The other module.
        :return: *True* if circular, *False* otherwise.
        :rtype: bool
        """
        return self.is_dependent(other) and other.is_dependent(self)

    @property
    def is_excluded(self):
        return self.name in Generator.excluded_mods

    def bind_templates(self, path):
        """
        Bind templates.
        :param str path: Path to write templates.
        :return:
        """
        # Create module folder
        if not os.path.isdir(path):
            os.makedirs(path)

        # Get ordered binders and generate source
        binders = self.templates
        for binder in binders:
            binder.bind(path)

    def bind(self, path):
        """
        Bind the module.
        :param str path: Path to write sub-directory.
        :return: None.
        """
        # Create module folder and main source file
        if not os.path.isdir(path):
            os.makedirs(path)
        fname = '/'.join([path, self.name + '.cxx'])

        # Check if module is split
        is_split = self.name in Generator.split

        fout = io.StringIO()

        # File header
        fout.write(SRC_PREFIX)

        # Generate binding source and headers
        binders = self.sorted_binders
        extra_headers = []
        for binder in binders:
            headers = binder.bind(path)
            if headers:
                extra_headers += headers

        # Write include files
        used_includes = set()
        inc_src = []
        for inc in self.includes + extra_headers:
            if inc in used_includes:
                continue
            used_includes.add(inc)
            line = '#include <{}>\n'.format(inc)
            fout.write(line)
            inc_src.append(line)
        fout.write('\n')

        # Write opaque types
        has_opaque = False
        for binder in binders:
            for opaque in binder.opaque:
                has_opaque = True
                fout.write(opaque)
        if has_opaque:
            fout.write('\n')

        # Write manual text before module
        before_mod_src = []
        if self.name in Generator.before_module:
            for txt in Generator.before_module[self.name]:
                fout.write('{}\n'.format(txt))
                before_mod_src.append(txt)
            fout.write('\n')

        # Write split function signature
        if is_split:
            fout.write('// Functions for split modules\n')
            fout.write('void bind_{}_2(py::module&);\n\n'.format(self.name))

        # Initialize
        fout.write('PYBIND11_MODULE({}, mod) {{\n\n'.format(self.name))

        # Import other modules
        has_guards = self.name in Generator.import_guards
        guarded = set()
        if has_guards:
            guarded = Generator.import_guards[self.name]
        for mod_name in self.imports:
            if mod_name in guarded:
                continue
            if mod_name != self.name:
                package_name = Generator.get_namespace(mod_name)
                fout.write('py::module::import(\"{}.{}\");\n'.format(
                    package_name, mod_name))
        fout.write('\n')

        # Import guards
        for mod_name in guarded:
            package_name = Generator.get_namespace(mod_name)
            fout.write('struct Import{}{{\n'.format(mod_name))
            fout.write(
                '\tImport{}() {{ py::module::import(\"{}.{}\"); }}\n'.format(
                    mod_name, package_name, mod_name))
            fout.write('};\n\n')

        # If the module is split in two, only bind half and save the rest for another file
        split_binders = []
        if is_split:
            indx = len(binders) // 2
            binders, split_binders = binders[:indx], binders[indx:]

        # Main bind loop
        src = []
        for binder in binders:
            src.extend(binder.src)

        # Patch the file
        # TODO: Line Number is off
        patch_src(self.name, src)

        # Write it out
        for line in src:
            fout.write(line)
        fout.write('\n')

        # Call the split function
        if is_split:
            line = 'bind_{}_2(mod);\n\n'.format(self.name)
            fout.write(line)

        # End module
        fout.write('}\n')
        overwrite_if_changed(fname, fout)

        # Create the split file
        if is_split:
            fname = '/'.join([path, self.name + '_2.cxx'])
            fout = io.StringIO()

            # File header
            fout.write(SRC_PREFIX)

            # Duplicate all the include files for now
            fout.writelines(inc_src)
            fout.write('\n')

            # Duplicate text before module
            if before_mod_src:
                fout.writelines(before_mod_src)
                fout.write('\n\n')

            # Function signature
            line = 'void bind_{}_2(py::module &mod)\n'.format(self.name)
            fout.write(line)
            fout.write('{\n\n')

            # Main bind loop
            src = []
            for binder in split_binders:
                src.extend(binder.src)

            # Patch the split file
            # TODO: Line Number is off
            patch_src(self.name, src)

            # Write it out
            for line in src:
                fout.write(line)
            fout.write('\n')

            # End module
            fout.write('}\n')
            overwrite_if_changed(fname, fout)


class CursorBinder(object):
    """
    Binder for cursors.
    :param clang.cindex.Cursor cursor: The underlying cursor.
    :ivar clang.cindex.Cursor cursor: The underlying cursor.
    :ivar binder.core.CursorBinder alias: The alias of this binder if
        applicable.
    :ivar str parent_name: The name of the binding parent.
    :ivar str python_name: Name for binder in Python if different than
        spelling.
    :ivar str bind_name: Function name for binding.
    :ivar list(str) includes: List of relevant include files for this binder.
    :ivar str module_name: The module name for this binder.
    :ivar str filename: The file where this binder is located.
    """

    def __init__(self, cursor):
        self.cursor = cursor
        self.alias = None
        self.parent_name = 'mod'
        self._pname = None
        self.bind_name = None
        self.includes = []
        self.grouped_binders = []
        self.skip = False
        self.src = []
        self.opaque = []
        self.macro = None

        # Filename
        try:
            fname = cursor.location.file.name
            fname = fname.replace('\\', '/').split('/')[-1]
        except AttributeError:
            fname = None
        self.filename = fname

        # Module name based on filename
        name = '__None__'
        if fname is not None:
            name = fname
            delimiter = '.'
            if '_' in name:
                delimiter = '_'
            name = name.split(delimiter)[0]
        self.module_name = name

    def __hash__(self):
        return self.cursor.hash

    def __eq__(self, other):
        return self.cursor.hash == other.cursor.hash

    def __repr__(self):
        return 'Cursor: {} ({})'.format(self.qualified_name, self.kind)

    @property
    def kind(self):
        """
        :return: The cursor kind.
        :rtype: clang.cindex.CursorKind
        """
        try:
            return self.cursor.kind
        except AttributeError:
            return CursorKind.NO_DECL_FOUND

    @property
    def type(self) -> "TypeBinder":
        """
        :return: The cursor type.
        :rtype: binder.core.TypeBinder
        """
        return TypeBinder(self.cursor.type)

    @property
    def canonical(self) -> "CursorBinder":
        """
        :return: The canonical cursor.
        :rtype: binder.core.CursorBinder
        """
        return CursorBinder(self.cursor.canonical)

    @property
    def underlying_typedef_type(self) -> "TypeBinder":
        """
        :return: The cursor underlying typedef type.
        :rtype: binder.core.TypeBinder
        """
        return TypeBinder(self.cursor.underlying_typedef_type)

    @property
    def rtype(self) -> "TypeBinder":
        """
        :return: The cursor result type.
        :rtype: binder.core.TypeBinder
        """
        return TypeBinder(self.cursor.result_type)

    @property
    def display_name(self) -> str:
        """
        :return: The display name.
        :rtype: str
        """
        try:
            return self.cursor.displayname
        except AttributeError:
            return 'NULL'

    @property
    def qualified_display_name(self) -> str:
        """
        :return: The qualified display name.
        :rtype: str
        """
        names = []
        b = self
        while not b.is_null and not b.is_tu:
            if name := b.display_name:
                names.append(name)
            else:
                break
            b = b.parent
        names.reverse()
        if "__1" in names:
            # Hack for std::__1::basic_ostream
            names.remove("__1")
        qname = '::'.join(names)

        if 'operator()' in qname:
            # Hack for call operator...
            qname = qname.split('()')[0]
            return ''.join([qname, '()'])

        return qname

    @property
    def spelling(self) -> str:
        """
        :return: The spelling.
        :rtype: str
        """
        return self.cursor.spelling

    @property
    def no_decl(self) -> bool:
        return self.kind == CursorKind.NO_DECL_FOUND

    @property
    def is_null(self) -> bool:
        return self.cursor is None or self.kind == CursorKind.NO_DECL_FOUND

    @property
    def is_tu(self) -> bool:
        return self.kind == CursorKind.TRANSLATION_UNIT

    @property
    def is_enum(self) -> bool:
        return self.kind == CursorKind.ENUM_DECL

    @property
    def is_enum_constant(self) -> bool:
        return self.kind == CursorKind.ENUM_CONSTANT_DECL

    @property
    def is_function(self) -> bool:
        return self.kind == CursorKind.FUNCTION_DECL

    @property
    def is_class(self) -> bool:
        return self.kind in (CursorKind.CLASS_DECL, CursorKind.STRUCT_DECL)

    @property
    def is_typedef(self) -> bool:
        return self.kind == CursorKind.TYPEDEF_DECL

    @property
    def is_cxx_base(self) -> bool:
        return self.kind == CursorKind.CXX_BASE_SPECIFIER

    @property
    def is_constructor(self) -> bool:
        return self.kind == CursorKind.CONSTRUCTOR

    @property
    def is_destructor(self) -> bool:
        return self.kind == CursorKind.DESTRUCTOR

    @property
    def is_cxx_method(self) -> bool:
        return self.kind == CursorKind.CXX_METHOD

    @property
    def is_param(self) -> bool:
        return self.kind == CursorKind.PARM_DECL

    @property
    def is_field(self) -> bool:
        return self.kind == CursorKind.FIELD_DECL

    @property
    def is_bitfield(self) -> bool:
        return self.cursor.is_bitfield()

    @property
    def is_template_ref(self) -> bool:
        return self.kind == CursorKind.TEMPLATE_REF

    @property
    def is_class_template(self) -> bool:
        return self.kind == CursorKind.CLASS_TEMPLATE

    @property
    def is_function_template(self) -> bool:
        return self.kind == CursorKind.FUNCTION_TEMPLATE

    @property
    def is_template_type_param(self):
        return self.kind == CursorKind.TEMPLATE_TYPE_PARAMETER

    @property
    def is_using_decl(self):
        return self.kind == CursorKind.USING_DECLARATION

    @property
    def is_type_alias_decl(self):
        return self.kind == CursorKind.TYPE_ALIAS_DECL

    @property
    def is_overloaded_decl_ref(self):
        return self.kind == CursorKind.OVERLOADED_DECL_REF

    @property
    def is_cxx_access_spec(self):
        return self.kind == CursorKind.CXX_ACCESS_SPEC_DECL

    @property
    def is_type_ref(self):
        return self.kind == CursorKind.TYPE_REF

    @property
    def is_namespace(self) -> bool:
        return self.kind == CursorKind.NAMESPACE

    @property
    def is_public(self):
        return not self.is_private and not self.is_protected

    @property
    def is_private(self):
        return self.cursor.access_specifier == AccessSpecifier.PRIVATE

    @property
    def is_protected(self):
        return self.cursor.access_specifier == AccessSpecifier.PROTECTED

    @property
    def is_definition(self):
        return self.cursor.is_definition()

    @property
    def is_virtual_method(self):
        return self.cursor.is_virtual_method()

    @property
    def is_pure_virtual_method(self):
        return self.cursor.is_pure_virtual_method()

    @property
    def is_abstract(self):
        return self.cursor.is_abstract_record()

    @property
    def has_unimplemented_methods(self):
        # TODO: Doesn't support overloads
        all_virtual_methods = set()
        all_methods = set()
        for m in self.methods:
            if m.is_pure_virtual_method:
                return True
            all_methods.add(m.spelling)

        for base in self._all_bases:
            base = base.get_definition()
            if base.is_null:
                continue
            for m in base.methods:
                if m.is_pure_virtual_method:
                    all_virtual_methods.add(m.spelling)
                else:
                    all_methods.add(m.spelling)
        return all_virtual_methods.difference(all_methods)

    @property
    def is_const_method(self):
        return self.cursor.is_const_method()

    @property
    def is_static_method(self):
        return self.cursor.is_static_method()

    @property
    def is_move_ctor(self):
        return self.cursor.is_move_constructor()

    @property
    def is_copy_ctor(self):
        return self.cursor.is_copy_constructor()

    @property
    def is_default_ctor(self):
        return self.cursor.is_default_constructor()

    @property
    def is_anonymous(self):
        return self.cursor.is_anonymous()

    @property
    def is_excluded(self):
        """
        :return: Check if the cursor is excluded.
        :rtype: bool
        """
        if self.is_enum:
            return self.qualified_name in Generator.excluded_enums
        elif self.is_function or self.is_constructor:
            name = self.qualified_name
            # Special case trying to exclude functions with certain signatures
            dname = self.qualified_display_name
            rtype = self.rtype.spelling
            return (any(fnmatch(name, pat) for pat in Generator.excluded_functions) or
                    any(fnmatch(dname, pat) for pat in Generator.excluded_functions) or
                    any(fnmatch(rtype, pat) for pat in Generator.excluded_rtypes))
        elif self.is_class or self.is_class_template:
            return self.qualified_name in Generator.excluded_classes
        elif self.is_typedef:
            return self.qualified_name in Generator.excluded_typedefs
        elif self.is_field:
            return self.qualified_name in Generator.excluded_fields
        elif self.is_cxx_method:
            # Check if method name or qualified name is excluded
            name, fname = self.qualified_name, self.spelling
            # Special case trying to exclude functions with certain signatures
            dname = self.qualified_display_name
            if self.is_static_method:
                name += '_'
                fname += '_'
            rtype = self.rtype.spelling
            return (any(fnmatch(name, pat) for pat in Generator.excluded_functions) or
                    any(fnmatch(fname, pat) for pat in Generator.excluded_fnames) or
                    any(fnmatch(dname, pat) for pat in Generator.excluded_functions) or
                    any(fnmatch(rtype, pat) for pat in Generator.excluded_rtypes))

        return False

    @property
    def is_transient(self):
        """
        :return: Check if cursor is either Standard_Transient type or derived
            from it.
        :rtype: bool
        """
        if self.type.spelling == 'Standard_Transient':
            return True
        bases = self._all_bases
        for base in bases:
            if base.type.spelling == 'Standard_Transient':
                return True
        return False

    @property
    def is_operator(self):
        if self.is_function or self.is_cxx_method:
            return self.spelling in PY_OPERATORS
        return False

    @property
    def is_getter_method(self):
        """
        Check if binder is a public method that returns a reference and the
        class has an another public method public method named "Set<name>".

        :return: *True* if getter, *False* otherwise.
        :rtype: bool
        """
        if self.is_cxx_method and self.is_public and self.rtype.is_lvalue:
            setter_name = f'Set{self.spelling}'
            for method in self.parent.methods:
                if method.spelling == setter_name and method.is_public:
                    return True
        return False

    @property
    def is_nested(self):
        """
        Check if binder is nested in a class, struct, or class template.
        :return: *True* if nested, *False* otherwise.
        :rtype: bool
        """
        parent = self.parent
        if parent.is_tu:
            return False
        return True

    @property
    def is_immutable(self):
        """
        :return: Check if the type is a Python immutable type.
        :rtype: bool
        """
        type_ = self.type
        if type_.is_pointer_like:
            type_ = type_.get_pointee()
        return type_.spelling in Generator.immutable

    @property
    def is_maybe_iterable(self):
        """
        :return: Check to see if the type is maybe iterable (has begin and end
            methods).
        :rtype: bool
        """
        method_names = set()
        for f in self.methods:
            if not f.is_public:
                continue
            method_names.add(f.spelling)

        return 'begin' in method_names and 'end' in method_names

    @property
    def qualified_name(self):
        """
        :return: The fully qualified displayed name.
        :rtype: str
        """
        names = []
        b = self
        while not b.is_null and not b.is_tu:
            name = b.display_name
            if name:
                names.append(b.display_name)
            else:
                break
            b = b.parent
        names.reverse()
        qname = '::'.join(names)

        if 'operator()' in qname:
            # Hack for call operator...
            qname = qname.split('()')[0]
            return ''.join([qname, '()'])
        # Don't return function interface portion
        elif '(' in qname:
            return qname.split('(')[0]
        else:
            return qname

    @property
    def qualified_spelling(self):
        """
        :return: The fully qualified spelling.
        :rtype: str
        """
        names = []
        b = self
        while not b.is_null and not b.is_tu:
            name = b.spelling
            if name:
                names.append(b.spelling)
            else:
                break
            b = b.parent
        names.reverse()
        return '::'.join(names)

    @property
    def python_name(self):
        """
        :return: The Python name. If *None* then the spelling is returned.
        :rtype: str
        """
        if self._pname is not None:
            return self._pname
        if self.is_nested:
            name = self.spelling
        else:
            name = self.qualified_spelling
        name = name.replace('::', '_')
        return name

    @python_name.setter
    def python_name(self, pname):
        self._pname = pname

    @property
    def parent(self):
        """
        :return: The parent binder.
        :rtype: binder.core.CursorBinder
        """
        return CursorBinder(self.cursor.semantic_parent)

    @property
    def docs(self):
        """
        :return: The docstring.
        :rtype: str
        """
        docs = str(self.cursor.brief_comment)
        docs = docs.replace('\n', ' ')
        docs = docs.replace('\"', '\'')
        return docs

    @property
    def bases(self):
        """
        :return: List of base classes.
        :rtype: list(binder.core.CursorBinder)
        """
        return list(self.get_children_of_kind(CursorKind.CXX_BASE_SPECIFIER))

    @cached_property
    def _all_bases(self):
        """
        :return: All base classes.
        :rtype: list(binder.core.CursorBinder)
        """

        def _get_bases(_c):
            for base in _c.bases:
                bases.append(base)
                # Get declaration
                _decl = base.type.get_declaration()
                if _decl.no_decl:
                    continue

                # Get bases of class template
                if _decl.is_class_template:
                    _get_bases(_decl)
                    continue

                # Get template specialization if possible
                _spec = _decl.get_specialization()
                if not _spec.no_decl and _spec.is_class_template:
                    _get_bases(_spec)
                    continue

                # Get bases of regular class
                if _decl.is_class:
                    _get_bases(_decl)
                    continue

                # Get bases of a typedef
                if _decl.is_typedef:
                    # Get underlying type if a typedef
                    # c = base.type.get_canonical().get_declaration()
                    _decl = _decl.underlying_typedef_type.get_declaration()
                    # Check for a template
                    _spec = _decl.get_specialization()
                    if not _spec.no_decl and _spec.is_class_template:
                        _get_bases(_spec)
                    else:
                        _get_bases(_decl)
                    continue

                # Should never get here
                warnings.warn('Failed to find a base for {}'.format(base.spelling), RuntimeWarning)

        bases = []
        # If "self" is a template specialization, try and get it first before
        # starting search for base classes.
        # Get template specialization if possible
        spec = self.get_specialization()
        if not spec.no_decl and spec.is_class_template:
            _get_bases(spec)
        else:
            _get_bases(self)
        return bases

    @property
    def ctors(self):
        """
        :return: List of constructors.
        :rtype: list(binder.core.CursorBinder)
        """
        return list(self.get_children_of_kind(CursorKind.CONSTRUCTOR))

    @property
    def dtors(self):
        """
        :return: List of destructors.
        :rtype: list(binder.core.CursorBinder)
        """
        return list(self.get_children_of_kind(CursorKind.DESTRUCTOR))

    @property
    def has_public_dtor(self):
        """
        :return: Check if the binder has a public destructor.
        :rtype: bool
        """
        return any(self.get_children_of_kind(CursorKind.DESTRUCTOR, True))

    @property
    def fields(self):
        """
        :return: List of fields.
        :rtype: list(binder.core.CursorBinder)
        """
        return list(self.get_children_of_kind(CursorKind.FIELD_DECL))

    @property
    def enums(self):
        """
        :return: List of enums.
        :rtype: list(binder.core.CursorBinder)
        """
        return list(self.get_children_of_kind(CursorKind.ENUM_DECL))

    @property
    def methods(self):
        """
        :return: List of class methods.
        :rtype: list(binder.core.CursorBinder)
        """
        return list(self.get_children_of_kind(CursorKind.CXX_METHOD))

    @property
    def nested_classes(self):
        """
        :return: List of nested classes.
        :rtype: list(binder.core.CursorBinder)
        """
        return list(self.get_children_of_kind(CursorKind.CLASS_DECL, CursorKind.STRUCT_DECL))

    @property
    def parameters(self):
        """
        :return: List of parameters.
        :rtype: list(binder.core.CursorBinder)
        """
        return list(self.get_children_of_kind(CursorKind.PARM_DECL))

    @property
    def default_value(self):
        """
        :return: A string representation of the default value if available
            (e.g., "=0"). If not available returns an empty string.
        :rtype: str
        """
        if not (self.is_param or self.is_template_type_param):
            return ''
        txt = ''
        for t in self.cursor.get_tokens():
            if t.spelling == ">>":
                txt += ">" # clang bug?
            else:
                txt += t.spelling
        if not txt or '=' not in txt:
            return ''
        return txt.split('=')[-1]

    @property
    def enum_constants(self):
        """
        :return: List of enum constant binders.
        :type: list(binder.core.CursorBinder)
        """
        return list(self.get_children_of_kind(CursorKind.ENUM_CONSTANT_DECL))

    @property
    def template_parameters(self):
        """
        :return: List of template parameters.
        :type: list(binder.core.CursorBinder)
        """
        return list(self.get_children_of_kind(
            CursorKind.TEMPLATE_TYPE_PARAMETER,
            CursorKind.TEMPLATE_NON_TYPE_PARAMETER
        ))

    @property
    def needs_nodelete(self):
        """
        :return: Check to see if class needs py::nodelete
        :rtype: bool
        """
        # Check for hidden destructor of this class
        for dtor in self.dtors:
            if not dtor.is_public:
                return True
        return False

    @property
    def holder_type(self):
        """
        :return: The primary holder type.
        :rtype: str
        """
        if self.is_transient:
            return 'opencascade::handle'
        return 'std::unique_ptr'

    @property
    def needs_inout_method(self):
        """
        :return: Check to see if the function should use a lambda to return
            non const immutable types.
        :rtype: bool
        """
        for a in self.parameters:
            if (a.is_immutable and not a.type.is_const_qualified and
                    a.type.is_pointer_like):
                return True
        return False

    @property
    def needs_default_ctor(self):
        """
        :return: Check to see if the cursor needs a default constructor.
        """
        if self.is_abstract:
            return False
        items = [self] + self._all_bases
        for item in items:
            item.get_definition()
            if item.ctors:
                return False
        return True

    def get_definition(self):
        """
        If the binder is a reference to a declaration or a declaration of
        some entity, return a binder that points to the definition of that
        entity.
        :return: The definition.
        :rtype: binder.core.CursorBinder
        """
        return CursorBinder(self.cursor.get_definition())

    def get_specialization(self):
        """
        If the binder is a specialization of a template, attempt to get the
        cursor of the template.
        :return: The definition.
        :rtype: binder.core.CursorBinder
        """
        spec = self.cursor.get_specialization()
        if not hasattr(spec, "_tu"):
            # TODO is this a python-clang bug in 20+ or are we doing something wrong here?
            # Hack to workaround
            # Traceback (most recent call last):
            # File "python3.13/site-packages/clang/cindex.py", line 2106, in visitor
            #    child._tu = self._tu
            # AttributeError: 'Cursor' object has no attribute '_tu
            spec._tu = self.cursor._tu
        return CursorBinder(spec)

    def get_children(self):
        """
        Get children of binder.
        :return: The children.
        :rtype: list(binder.core.CursorBinder)
        """
        for child in self.cursor.get_children():
            yield CursorBinder(child)

    def get_children_of_kind(self, *kind, only_public=False):
        """
        Get children of a specified kind.
        :param clang.cindex.CursorKind kind: The cursor kind.
        :param bool only_public: Return only cursor that are public.
        :return: List of children.
        :rtype: Generator(binder.core.CursorBinder)
        """
        for c in self.get_children():
            if c.kind not in kind:
                continue
            if only_public and not c.is_public:
                continue
            yield c

    def dfs(self):
        """
        Depth-first walk of all descendants.
        :return: List of descendants.
        :rtype: Generator(binder.core.CursorBinder)
        """
        for cursor in self.cursor.walk_preorder():
            if not cursor.kind.is_translation_unit():
                yield CursorBinder(cursor)

    def build_includes(self):
        """
        Get a list of relevant files to include for the binder.
        :return: List of include files.
        :rtype: list(str)
        """
        includes = []

        # Extra headers
        qname = self.qualified_name
        if qname in Generator.plus_headers:
            for f in Generator.plus_headers[qname]:
                if f not in includes:
                    includes.append(f)

        # Traverse the binder and look for any type references.
        for item in self.dfs():
            if not item.is_type_ref and not item.is_template_ref:
                continue

            # Check valid file
            f = item.get_definition().filename
            if f is None:
                continue

            # Check available
            if f not in Generator.available_incs:
                continue

            # Check for excluded
            if f in Generator.excluded_headers:
                continue

            # Check for minus
            if qname in Generator.minus_headers:
                if f in Generator.minus_headers[qname]:
                    continue

            # Check duplicate
            if f not in includes:
                includes.append(f)

        # Add file for this type
        f = self.filename
        if f not in includes:
            includes.append(f)

        # Replace any .lxx or .gxx with .hxx
        for inc in includes:
            if '.lxx' in inc:
                msg = '\tReplacing include extension for {}: {}.\n'.format(
                    qname, inc)
                logger.write(msg)
                inc = inc.replace('.lxx', '.hxx')
            elif '.gxx' in inc:
                msg = '\tReplacing include extension for {}: {}.\n'.format(
                    qname, inc)
                logger.write(msg)
                inc = inc.replace('.gxx', '.hxx')

            # Check for duplicate
            if inc in self.includes:
                continue

            # Add include
            self.includes.append(inc)

        return self.includes

    def bind(self, path):
        """
        Bind the type.
        :return: None.
        """
        logger.write('\tBinding {}.\n'.format(self.qualified_spelling))
        if self.is_enum:
            return bind_enum(self)
        elif self.is_function:
            return bind_function(self)
        elif self.is_class:
            return bind_class(self)
        elif self.is_typedef:
            return bind_typedef(self)
        elif self.is_class_template:
            bind_class_template(self, path)
        else:
            logger.write('\tUnsupported {}.\n'.format(self.qualified_spelling))

    def generate(self):
        """
        Generate the source text for the binder.
        :return: The source text.
        :rtype: list(str)
        """
        if self.is_enum:
            return generate_enum(self)
        elif self.is_function:
            return generate_function(self)
        elif self.is_class:
            return generate_class(self)
        elif self.is_typedef:
            return generate_typedef2(self)
        elif self.is_class_template:
            return generate_class_template(self)
        elif self.is_cxx_method:
            return generate_method(self)
        elif self.is_field:
            return generate_field(self)
        elif self.is_constructor:
            return generate_ctor(self)
        return []


class TypeBinder(object):
    """
    Binder for types.
    :param clang.cindex.Type type_: The type.
    :ivar clang.cindex.Type type: The underlying type.
    """

    def __init__(self, type_):
        self.type = type_

    def __repr__(self):
        return 'Type: {} ({})'.format(self.spelling, self.kind)

    @property
    def spelling(self):
        """
        :return: The spelling.
        :rtype: str
        """
        return self.type.spelling

    @property
    def alias_spelling(self):
        """
        :return: The spelling of the aliased type (using).
        :rtype: str
        """
        canonical_spelling = self.get_canonical().spelling
        # Hack for `const Standard_Address`, `const Standard_Address &`, `const Standard_CString &`
        if canonical_spelling in (
            "void *const",
            "void *const &",
            "const char *const &",
            "const std::string &",
        ):
            return canonical_spelling

        spelling = default_spelling = self.spelling
        if self.is_pointer_like:
            ref = self.get_pointee()
            if self.is_pointer:
                c = "*"
            elif self.is_lvalue:
                c = "&"
            else:
                c = "&&"
            spelling = f"{ref.alias_spelling}{c}"
        else:
            d = self.get_declaration()
            if d.is_enum:
                spelling = d.qualified_display_name
            elif d.is_class and d.is_nested:
                spelling = f"typename {d.qualified_display_name}"
            elif d.is_class: # and "handle<" in spelling and "opencascade::handle<" not in spelling:
                # TODO: spelling sometimes has template args but qualified doesnt?
                display_name = d.qualified_display_name
                if len(display_name) > len(spelling):
                    spelling = display_name
            elif d.is_typedef:
                underlying_type = d.underlying_typedef_type
                if underlying_type.is_fn_ptr:
                    spelling = self.get_canonical().spelling
                else:
                    spelling = underlying_type.alias_spelling
            elif d.is_using_decl or d.is_type_alias_decl:
                toks = []
                MODIFIERS = ("const", "long", "unsigned", "typename")
                all_toks = [it.spelling for it in d.cursor.get_tokens()]
                for i, v in enumerate(all_toks):
                    # Keep space after const and long
                    if v in MODIFIERS:
                        toks.append(f"{v} ")
                        continue
                    # HACK...
                    last_tok = toks[-1] if toks else None
                    next_tok = all_toks[i+1] if i+1 < len(all_toks) else None
                    if v == "handle" and next_tok == "<" and last_tok != ":":
                        toks.append("opencascade::handle")
                        continue
                    toks.append(v)
                spelling = "".join(toks[3:])

        if self.is_const_qualified and not spelling.startswith("const "):
            if default_spelling.endswith("*const") and spelling.endswith("*"):
                # Apparently there is a difference between 'const T*' and 'T *const'
                return f"{spelling[:-1]} *const"
            return f"const {spelling}"
        elif canonical_spelling.endswith("*const &") and spelling.endswith("*&") and spelling.startswith("const "):
            #  HACK Replace 'const T*&' with 'T *const &'
            return f"{spelling[6:-2]} *const &"

        return spelling

    @property
    def kind(self):
        """
        :return: The cursor kind.
        :rtype: clang.cindex.CursorKind
        """
        return self.type.kind

    @property
    def is_null(self):
        return self.type is None or self.kind == TypeKind.INVALID

    @property
    def is_record(self):
        return self.kind == TypeKind.RECORD

    @property
    def is_typedef(self):
        return self.kind == TypeKind.TYPEDEF

    @property
    def is_pointer(self):
        return self.kind == TypeKind.POINTER

    @property
    def is_elaborated(self):
        return self.kind == TypeKind.ELABORATED

    @property
    def is_lvalue(self):
        return self.kind == TypeKind.LVALUEREFERENCE

    @property
    def is_rvalue(self):
        return self.kind == TypeKind.RVALUEREFERENCE

    @property
    def is_fnproto(self):
        return self.kind == TypeKind.FUNCTIONPROTO

    @property
    def is_fn_ptr(self):
        if not self.is_pointer:
            return False
        return self.get_pointee().is_fnproto

    @property
    def is_pointer_like(self):
        return self.is_pointer or self.is_lvalue or self.is_rvalue

    @property
    def is_array_like(self):
        return self.kind in [TypeKind.CONSTANTARRAY, TypeKind.INCOMPLETEARRAY,
                             TypeKind.VARIABLEARRAY,
                             TypeKind.DEPENDENTSIZEDARRAY]

    @property
    def is_const_qualified(self):
        return self.type.is_const_qualified()

    @property
    def is_opaque(self):
        """ An opaque type with no known definition """
        if self.is_pointer:
            spelling = self.spelling
            if spelling.startswith("struct "):
                spelling = spelling[7:]
            if spelling.strip("* ") in Generator.opaque_types:
                return True
        elif self.is_alias:
            spelling = self.alias_spelling
            if spelling.startswith("struct "):
                spelling = spelling[7:]
            if spelling.endswith("*") and spelling.strip("* ") in Generator.opaque_types:
                return True
        return False

    @property
    def is_alias(self):
        if self.is_elaborated:
            d = self.get_declaration()
            return (
                d.is_type_alias_decl
                or d.is_using_decl
                or d.is_typedef
                or d.is_class
                or d.is_enum
            )
        elif self.is_pointer_like:
            try:
                return self.get_pointee().is_alias
            except RecursionError:
                pass
        return False

    def get_declaration(self):
        """
        Get the declaration of the type.
        :return: The declaration.
        :rtype: binder.core.CursorBinder
        """
        return CursorBinder(self.type.get_declaration())

    def get_canonical(self):
        """
        Get the canonical type.
        :return: The canonical type.
        :rtype: binder.core.TypeBinder
        """
        return TypeBinder(self.type.get_canonical())

    def get_pointee(self):
        """
        For pointer types, returns the type of the pointee.
        :return: The pointee.
        :rtype: binder.core.TypeBinder
        """
        return TypeBinder(self.type.get_pointee())


def bind_enum(binder):
    """
    Bind an enum.
    :param binder.core.CursorBinder binder: The binder.
    :return: None.
    """
    src = ['// ENUM: {}\n'.format(binder.python_name.upper())]

    # Get list of binders if grouped
    binders = [binder] + binder.grouped_binders

    # Bind function name
    python_name = binder.python_name
    # Hack for "anonymous" enums
    if not python_name or '(anonymous enum' in python_name:
        msg = '\tFound anonymous enum: {}\n'.format(binder.type.spelling)
        logger.write(msg)

    # Generate source
    for binder_ in binders:
        src += generate_enum(binder_)
        src.append('\n')
    src.append('\n')

    binder.src = src
    return []


def bind_function(binder):
    """
    Bind a function.
    :param binder.core.CursorBinder binder: The binder.
    :return: None.
    """
    src = ['// FUNCTION: {}\n'.format(binder.python_name.upper())]

    # Get list of binders if function is overloaded
    binders = [binder] + binder.grouped_binders

    # Generate source
    for binder_ in binders:
        src += generate_function(binder_)

    binder.src = src
    return []


def bind_class(binder):
    """
    Bind a class.
    :param binder.core.CursorBinder binder: The binder.
    :return: None.
    """
    src = ['// CLASS: {}\n'.format(binder.python_name.upper())]

    # Generate source
    src += generate_class(binder)
    src.append('\n')

    binder.src = src

    # Add extra header for special macro case
    if binder.macro is None:
        return []
    return binder.macro.headers()


def bind_typedef(binder):
    """
    Bind a typedef.
    :param binder.core.CursorBinder binder: The binder.
    :return: None.
    """
    src = ['// TYPEDEF: {}\n'.format(binder.python_name.upper())]

    # Text before type
    qname = binder.qualified_name
    if qname in Generator.before_type:
        src.append('// Before type\n')
        for txt in Generator.before_type[qname]:
            txt = ''.join([txt, '\n'])
            src.append(txt)
        src.append('\n')

    # Generate source
    other_src, bind_template, extra = generate_typedef2(binder)

    # Comment if excluded (check if already commented in case the typedef is an
    # already commented out class definition.
    if (binder.is_excluded or (
        other_src
        and (
            other_src[0].startswith("bind_opencascade::handle")
            or other_src[0].startswith("bind_std::")
        )
    )):
        other_src.insert(0, '/*\n')
        if other_src[-1] != '*/\n':
            other_src.append('*/\n')
    src += other_src
    src.append('\n')

    # Include template if needed
    extra_headers = []
    if bind_template is None:
        bind_template = []
    for template in bind_template:
        if template in Generator.available_templates:
            extra_headers.append(template + '.hxx')

    # Add opaque defs
    for txt in extra:
        binder.opaque.append(txt)

    binder.src = src
    return extra_headers


def bind_class_template(binder, path):
    """
    Bind a class template.
    :param binder.core.CursorBinder binder: The binder.
    :param str path: The path to write the source file.
    :return: None.
    """
    # Bind function name
    bind_name = f"bind_{binder.python_name}"

    # Include guard
    src = ['#pragma once\n']

    # Include files
    includes = list(Generator.common_includes) + binder.includes
    if extra_includes := Generator.plus_headers.get(bind_name):
        includes.extend(extra_includes)
    for inc in sorted(set(includes)):
        src.append(f'#include <{inc}>\n')
    src.append('\n')

    # Bind function name
    binder.bind_name = bind_name
    Generator.available_templates.add(binder.bind_name)

    # Function template
    template_params = []
    for t in binder.template_parameters:
        if t.is_template_type_param:
            template_param = f'typename {t.display_name}'
        else:
            template_param = '{} {}'.format(t.type.spelling, t.display_name)
        if t.default_value:
            template_param += '={}'.format(t.default_value)
        template_params.append(template_param)
    src.append('template <{}>\n'.format(', '.join(template_params)))

    # Bind function
    src.append(
        'void {}(py::module &mod, std::string const &name, py::module_local const &local){{\n\n'.format(
            bind_name))

    # Generate source
    src += generate_class_template(binder)
    src.append('\n')

    # End function
    src.append('}\n\n')

    # Patch the file
    patch_src(bind_name, src)

    # Write file
    fname = ''.join([path, '/', bind_name, '.hxx'])

    fout = io.StringIO()
    fout.write(SRC_PREFIX)
    fout.writelines(src)
    overwrite_if_changed(fname, fout)


def generate_enum(binder):
    """
    Generate source for enumeration.
    :param binder.core.CursorBinder binder: The binder.
    :return: List of binder source lines.
    :rtype: list(str)
    """
    src = []

    # Names
    qname = binder.qualified_name
    parent = binder.parent_name
    docs = binder.docs

    name = binder.python_name
    if qname in Generator.python_names:
        name = Generator.python_names[qname]

    # Cast anonymous enum to int
    if re.search(r'(anonymous|unnamed) enum', binder.type.spelling):
        for e in binder.enum_constants:
            name, qname = e.spelling, e.qualified_name
            # Check and fix if anonymous is in the enum decl
            m = re.search(r'\((anonymous|unnamed)', e.type.spelling)
            if m:
                indx = m.span()[0]
                if indx:
                    prefix = e.type.spelling[:indx]
                    qname = ''.join([prefix, e.spelling])

            txt = '{}.attr(\"{}\") = py::cast(int({}));\n'.format(parent,
                                                                  name,
                                                                  qname or name)
            src.append(txt)
    else:
        # Hack for missing name
        if not name:
            name = binder.type.spelling
            name = name.replace('::', '_')

        # Hack to handle ::enum_constant
        if not qname:
            qname = binder.type.spelling

        # Source
        txt = 'py::enum_<{}>({}, \"{}\", \"{}\")\n'.format(qname, parent, name,
                                                           docs)
        src.append(txt)
        for e in binder.enum_constants:
            # Hack to handle ::enum_constant or missing qualified name
            qname = e.qualified_name
            if qname.startswith('::'):
                qname = ''.join([binder.type.spelling, qname])
            elif '::' not in qname:
                qname = '::'.join([e.type.spelling, e.spelling])
            txt = '\t.value(\"{}\", {})\n'.format(e.spelling, qname)
            src.append(txt)
        src.append('\t.export_values();\n')

    # Comment if excluded
    if binder.is_excluded:
        src.insert(0, '/*\n')
        src.append('*/\n')

    return src


def generate_function(binder):
    """
    Generate source for function.
    :param binder.core.CursorBinder binder: The binder.
    :return: Binder source as a list of lines.
    :rtype: list(str)
    """
    # Names
    fname = binder.spelling
    qname = binder.qualified_name
    docs = binder.docs

    rtype = binder.rtype.spelling
    if binder.rtype.is_opaque:
        rtype = f"void* /* {rtype} */"

    _, _, _, signature, _, is_array_like = function_signature(binder)
    if signature:
        signature = ', '.join(signature)
    else:
        signature = ''

    if binder.is_excluded:
        prefix = '// excluded // '
    else:
        prefix = ''

    # Variable names and default values
    args = []
    for arg in binder.parameters:
        default_value = arg.default_value
        if default_value:
            default_value = '=' + default_value
        args.append('py::arg(\"{}\"){}'.format(arg.spelling, default_value))
    if args:
        args = ', ' + ', '.join(args)
    else:
        args = ''


    # Source
    interface = '({} (*) ({}))'.format(rtype, signature)
    src = ['{}mod.def(\"{}\", {} &{}, \"{}\"{});\n\n'.format(
        prefix, fname, interface, qname, docs, args)]

    # TODO How to handle arrays
    if True in is_array_like:
        src[0] = ' '.join(['//', src[0]])

    return src


def generate_class(binder):
    """
    Generate source for class.
    :param binder.core.CursorBinder binder: The binder.
    :return: Binder source as a list of lines.
    :rtype: list(str)
    """
    # Special handling of certain macro types
    macro = binder.macro
    if macro is not None:
        src = [macro.generate(), '\n']
        return src

    # Don't bind if it doesn't have any children.
    if not any(binder.get_children()):
        logger.write(
            '\tNot binding class: {}\n'.format(binder.python_name))
        return []

    # Names
    name = binder.python_name
    qname = binder.qualified_name
    docs = binder.docs

    src = []

    # Text before type
    if qname in Generator.before_type:
        src.append('// Before type\n')
        for txt in Generator.before_type[qname]:
            txt = ''.join([txt, '\n'])
            src.append(txt)
        src.append('\n')

    # Source variable
    source_name = Generator.python_names.get(qname, name)
    if binder.is_nested:
        cls = '_'.join(['cls', binder.parent.python_name, source_name])
    else:
        cls = '_'.join(['cls', source_name])

    # Holder
    if binder.is_transient:
        holder = ', opencascade::handle<{}>'.format(qname)
        holder_type = 'opencascade::handle'
    elif binder.needs_nodelete or qname in Generator.nodelete:
        holder = ', std::unique_ptr<{}, py::nodelete>'.format(qname)
        holder_type = 'std::unique_ptr'
    else:
        # Use default std::unique_ptr
        holder = ''
        holder_type = 'std::unique_ptr'

    # Excluded base classes
    base_names = []
    bases_classes = binder.bases
    if qname in Generator.excluded_bases:
        excluded_bases = Generator.excluded_bases[qname]
    elif name in Generator.excluded_bases:
        excluded_bases = Generator.excluded_bases[name]
    else:
        excluded_bases = []

    # Base classes
    for base in bases_classes:
        if not base.is_public:
            continue
        name = base.type.spelling
        if name in excluded_bases or name in Generator.excluded_classes:
            continue
        # A total hack to try and figure out holder type. Might be a libclang
        # issue. Seems like the same cursor comes back from both methods but
        # only one returns an 'opencascade::handle' holder type. So, if either
        # of them returns that just use it.
        holder1 = base.type.get_declaration().holder_type
        holder2 = base.type.get_canonical().get_declaration().holder_type
        base_holder_type = 'std::unique_ptr'
        if 'opencascade::handle' in [holder1, holder2]:
            base_holder_type = 'opencascade::handle'
        # Check to see if type uses same holder type
        if holder_type != base_holder_type:
            msg = '\tMismatched holder types: {} --> {}\n'.format(
                binder.spelling, name
            )
            logger.write(msg)
            continue
        base_names.append(name)

    if base_names:
        bases = ', ' + ', '.join(base_names)
    else:
        bases = ''

    # Check py::multiple_inheritance
    multi_base = ''
    if len(bases_classes) > 1 and (len(base_names) < len(bases_classes)):
        multi_base = ', py::multiple_inheritance()'

    # Name will be given if binding a class template
    if binder.is_class_template:
        name_ = 'name.c_str()'
    else:
        name_ = '\"{}\"'.format(binder.python_name)
    if qname in Generator.python_names:
        name_ = '\"{}\"'.format(Generator.python_names[qname])

    # Module or parent name for nested classes
    parent = 'mod'
    if binder.is_nested and qname in Generator.nested_classes:
        parent = 'cls_' + binder.parent.python_name

    # Use py::module_local() for aliases
    local = ''
    if binder.is_class_template or binder.parent.is_class_template:
        local = ', local'
    elif binder.alias is not None:
        local = ', py::module_local()'

    # Source
    tname = 'typename ' + qname if '::' in qname else qname
    src.append('py::class_<{}{}{}> {}({}, {}, \"{}\"{}{});\n'.format(
        tname, holder, bases, cls, parent, name_, docs, multi_base,
        local))

    # Constructors
    src_ctor = []
    if not binder.is_abstract:
        for item in binder.ctors:
            if item.is_public:
                item.parent_name = cls
                src_ctor += generate_ctor(item)

        # Check for default constructor
        if not src_ctor and binder.needs_default_ctor \
                and qname not in Generator.excluded_functions:
            src_ctor = ['{}.def(py::init<>());\n'.format(cls)]

            # If it has virtual methods tag it as abstract
            if binder.has_unimplemented_methods:
                src_ctor[0] = '// abstract virtual methods // ' + src_ctor[0]

    if src_ctor:
        src_ctor.insert(0, '\n// Constructors\n')
        src += src_ctor

    # Fields
    src_fields = []
    for item in binder.fields:
        if item.is_public:
            item.parent_name = cls
            src_fields += generate_field(item)
    if src_fields:
        src_fields.insert(0, '\n// Fields\n')
        src += src_fields

    # Methods
    src_methods = []
    for item in binder.methods:
        if item.is_public:
            item.parent_name = cls
            # TODO: Determine macro fn's eg  'vtkTypeMacro'
            src_methods += generate_method(item)
    if src_methods:
        src_methods.insert(0, '\n// Methods\n')
        src += src_methods

    # Check for an iterable type and add __iter__
    if binder.is_maybe_iterable:
        msg = '\tAdding __iter__ to {}\n'.format(qname)
        logger.write(msg)
        src += '{}.def(\"__iter__\", [](const {} &self) {{ return py::make_iterator(self.begin(), self.end()); }}, py::keep_alive<0, 1>());\n'.format(
            cls, qname)

    # Enums
    src_enums = []
    for item in binder.enums:
        if item.is_public:
            item.parent_name = cls
            src_enums += generate_enum(item)
    if src_enums:
        src_enums.insert(0, '\n// Enums\n')
        src += src_enums

    # Nested classes
    has_nested = False
    nested_classes = binder.nested_classes
    for nested in nested_classes:
        if nested.qualified_name not in Generator.nested_classes:
            continue
        if not nested.is_public:
            continue
        if not has_nested:
            src.append('\n// Nested classes\n')
            has_nested = True
        src += generate_class(nested)

    # Extra text for the class
    if qname in Generator.after_type:
        src.append('\n' + '// After type' + '\n')
        for txt in Generator.after_type[qname]:
            src.append(txt)
            src.append('\n')

    # Comment if excluded
    if binder.is_excluded:
        src.insert(0, '/*\n')
        src.append('*/\n')

    return src


def generate_ctor(binder):
    """
    Generate source for class constructor.
    :param binder.core.CursorBinder binder: The binder.
    :return: Binder source as a list of lines.
    :rtype: list(str)
    """
    ctors = []

    sig = function_signature(binder)
    nargs, ndefaults, args_name, args_type, defaults, is_array_like = sig

    for i in range(nargs - ndefaults, nargs + 1):
        names = args_name[0:i]
        types = args_type[0:i]

        signature = ', '.join(types)

        py_args = []
        for name in names:
            py_args.append(', py::arg(\"{}\")'.format(name))
        py_args = ''.join(py_args)

        src = '{}.def(py::init<{}>(){});\n'.format(binder.parent_name,
                                                   signature, py_args)
        # Comment if excluded
        if binder.is_excluded or binder.is_move_ctor or "&&" in signature:
            src = ' '.join(['//', src])
        ctors.append(src)

    return ctors


def generate_field(binder):
    """
    Generate source for class member fields.
    :param binder.core.CursorBinder binder: The binder.
    :return: Binder source as a list of lines.
    :rtype: list(str)
    """
    prefix = binder.parent_name
    name = binder.spelling
    qname = binder.qualified_name
    docs = binder.docs
    type_ = 'readwrite'
    if binder.type.is_const_qualified:
        type_ = 'readonly'

    if binder.is_excluded:
        prefix = '// {}'.format(prefix)

    if binder.is_bitfield:
        ftype = binder.type.spelling
        parent = binder.parent.qualified_name
        reader = '[](%s& self) -> %s { return self.%s; }' % (
            parent, ftype, name)
        if type_ == "_readonly":
            src = [
                '{}.def_property_readonly(\"{}\", {}, \"{}\");\n'.format(
                    prefix, name, reader, docs)
            ]
        else:
            writer = '[](%s& self, const %s value) { self.%s = value; }' % (
                parent, ftype, name)
            src = [
                '{}.def_property(\"{}\", {}, {}, \"{}\");\n'.format(
                    prefix, name, reader, writer, docs)
            ]
    else:
        src = [
            '{}.def_{}(\"{}\", &{}, \"{}\");\n'.format(
                prefix, type_, name, qname, docs)
        ]
    # TODO: Make setter/getter for bitfield
    if binder.type.is_array_like:
        src[0] = ' '.join(['//', src[0]])

    return src


def generate_method(binder):
    """
    Generate source for class member function.
    :param binder.core.CursorBinder binder: The binder.
    :return: Binder source as a list of lines.
    :rtype: list(str)
    """
    methods = []

    prefix = '{}'.format(binder.parent_name)

    if binder.is_static_method:
        is_static = '_static'
    else:
        is_static = ''

    fname = binder.spelling
    if is_static:
        fname += '_'

    # Comment if excluded
    if binder.is_excluded:
        prefix = '// excluded // {}'.format(prefix)
    elif binder.is_pure_virtual_method:
        prefix = '// virtual // {}'.format(prefix)

    if binder.rtype.is_opaque:
        rtype = f"void* /* {binder.rtype.spelling} */"
    elif binder.rtype.is_alias:
        rtype = binder.rtype.alias_spelling
    else:
        rtype = binder.rtype.spelling


    qname = binder.qualified_name

    ptr = '*'
    if not binder.is_static_method:
        ptr = '::'.join([binder.parent.qualified_name, '*'])

    if binder.is_const_method:
        is_const = ' const'
    else:
        is_const = ''

    docs = binder.docs

    # Operators
    is_operator = ''
    if binder.is_operator:
        fname = PY_OPERATORS[fname]
        is_operator = 'py::is_operator(), '

    sig = function_signature(binder)
    nargs, ndefaults, args_name, args_type, defaults, is_array_like = sig

    # Return policy
    return_policy = ''
    if qname in Generator.return_policies:
        return_policy = ', py::return_value_policy::{}'.format(Generator.return_policies[qname])
    elif binder.is_getter_method:
        return_policy = ', py::return_value_policy::reference_internal'

    keep_alive = ''
    if qname in Generator.keep_alive:
        keep_alive = ', py::keep_alive<{}>()'.format(Generator.keep_alive[qname])

    # Call guards
    cguards = ''
    if qname in Generator.call_guards:
        cguards = ', ' + ', '.join(Generator.call_guards[qname])

    needs_inout = binder.needs_inout_method

    for i in range(nargs - ndefaults, nargs + 1):
        if needs_inout:
            txt = generate_immutable_inout_method(binder, qname)
            py_args = []
            for name in args_name:
                py_args.append(', py::arg(\"{}\")'.format(name))
            py_args = ''.join(py_args)
            src = '{}.def{}(\"{}\", {}, \"{}\"{});\n'.format(prefix, is_static,
                                                             fname, txt,
                                                             docs, py_args)
            if True in is_array_like:
                src = ' '.join(['//', src])
            return [src]
        elif i == nargs:
            names = args_name[0:i]
            types = args_type[0:i]

            signature = ', '.join(types)

            py_args = []
            for name in names:
                py_args.append(', py::arg(\"{}\")'.format(name))
            py_args = ''.join(py_args)

            src = '{}.def{}(\"{}\", ({} ({})({}){}) &{}, {}\"{}\"{}{}{}{});\n'.format(
                prefix, is_static,
                fname, rtype, ptr,
                signature, is_const,
                qname, is_operator,
                docs, py_args, return_policy, keep_alive, cguards)
        else:
            arg_list = []
            args_spelling = []
            for j in range(0, i):
                arg_list.append(args_type[j])
                args_spelling.append(args_name[j])

            if is_static:
                signature = ''
            else:
                parts = qname.split('::')
                parent_spelling = '::'.join(parts[:-1])
                signature = parent_spelling + ' &self'

            k = 0
            call_args = []
            for arg_type_spelling in arg_list:
                if not signature:
                    signature += '{} {}'.format(arg_type_spelling,
                                                'a' + str(k))
                else:
                    signature += ', {} {}'.format(arg_type_spelling,
                                                  'a' + str(k))
                call_args.append('a' + str(k))
                k += 1
            call = ', '.join(call_args)

            if not is_static:
                qname_ = 'self.' + fname
            else:
                qname_ = qname

            src = '{}.def{}(\"{}\", []({}) -> {} {{ return {}({}); }}{});\n'.format(
                prefix, is_static, fname, signature, rtype, qname_, call,
                cguards)

            if is_operator:
                # Hack disable self.__call__
                src = ' '.join(['//', src])

        # TODO How to handle arrays or &&?
        if True in is_array_like or "&&" in signature:
            src = ' '.join(['//', src])

        methods.append(src)

    return methods


def generate_typedef2(binder):
    """
    Generate source for a typedef.
    :param binder.core.CursorBinder binder: The binder.
    :return: Binder source as a list of lines and extra headers if needed.
    :rtype: tuple(list(str), list(str))
    """
    # Bind an alias in the same module
    alias = binder.alias
    if alias is not None and binder.module_name == alias.module_name:
        src = [
            'if (py::hasattr(mod, \"{}\")) {{\n'.format(alias.python_name),
            '\tmod.attr(\"{}\") = mod.attr(\"{}\");\n'.format(
                binder.python_name, alias.python_name),
            '}\n'
        ]
        return src, None, []

    # Bind class
    type_ = binder.type.get_canonical()
    decl = type_.get_declaration()
    template = decl.get_specialization()
    decl.alias = alias
    local = ', py::module_local(false)'
    if alias is not None:
        local = ', py::module_local()'
    if type_.is_record and template.is_class_template:
        if type_.spelling.startswith('std::vector'):
            txt = type_.spelling, binder.parent_name, binder.python_name
            src = ['py::bind_vector<{}>({}, \"{}\");\n'.format(*txt)]
            extra = ['PYBIND11_MAKE_OPAQUE({})\n'.format(txt[0])]
            return src, [], extra
        else:
            src = ['bind_{}({}, \"{}\"{});\n'.format(type_.spelling,
                                                     binder.parent_name,
                                                     binder.python_name,
                                                     local)]
            return src, ['bind_{}'.format(decl.spelling)], []

    elif type_.is_record and decl.is_class:
        decl.python_name = binder.spelling
        src = generate_class(decl)
        return src, [], []

    logger.write(
        '\tNot binding typedef: {}\n'.format(binder.python_name))
    return [], [], []


def patch_typenames(binder, src):
    """
    Hack to correct spelling of some types that miss the template parameters
    and "typename" qualifier like "NCollection_List::iterator" should be
    "typename NCollection_List<TheItemType>::iterator".
    :param binder.core.CursorBinder binder: The binder.
    :param str src: The class souce code
    :return: Binder source as a list of lines.
    :rtype: list(str)
    """
    src_out = []
    qname = binder.qualified_name
    spelling = binder.qualified_spelling

    find, replace = spelling + '::', 'typename ' + qname + '::'
    for line in src:
        line = line.replace(find, replace)
        src_out.append(line)
    return src_out


def generate_class_template(binder):
    """
    Generate source for a class template.
    :param binder.core.CursorBinder binder: The binder.
    :return: Binder source as a list of lines.
    :rtype: list(str)
    """
    src = generate_class(binder)
    src_out = patch_typenames(binder, src)
    return src_out


def function_signature(binder):
    """
    Generate data for the function signature.
    :param binder.core.CursorBinder binder: The binder.
    :return: Number of arguments, number of default values, list of names,
        list of types, their default values, and if their type is array-like.
    :rtype: tuple(int, int, list(str), list(str), list(str), list(bool))
    """
    args_name, args_type, defaults, is_array = [], [], [], []
    nargs, ndefaults = 0, 0

    for arg in binder.parameters:
        nargs += 1
        args_name.append(arg.spelling)
        if arg.type.is_opaque:
            args_type.append(f"void* /* {arg.type.spelling} */")
        elif arg.type.is_alias:
            args_type.append(arg.type.alias_spelling)
        else:
            args_type.append(arg.type.spelling)
        default = arg.default_value
        defaults.append(default)
        if default:
            ndefaults += 1
        if arg.type.is_array_like:
            is_array.append(True)
        else:
            is_array.append(False)

    return nargs, ndefaults, args_name, args_type, defaults, is_array


def generate_immutable_inout_method(binder, qname):
    """
    Generate binding for a function that modifies immutable types in place.
    :param binder.core.CursorBinder binder: The binder.
    :param str qname: The function fully qualified name.
    :return: The binding text.
    :rtype: str
    """
    logger.write('\tInout: {}\n'.format(qname))

    # Separate const and non-const input arguments
    args = []
    non_const_immutable_args = []
    i = 0
    for arg in binder.parameters:
        if arg.type.is_alias:
            type_ = arg.type.alias_spelling
        else:
            type_ = arg.type.spelling
        name = arg.spelling
        if not name:
            name = 'a{}'.format(str(i))
            i += 1
        if not arg.type.is_const_qualified and arg.is_immutable and arg.type.is_pointer_like:
            non_const_immutable_args.append((type_, name))
        args.append((type_, name))

    # All arguments
    is_static = True
    if args:
        delimiter = ', '
    else:
        delimiter = ''
    if binder.is_static_method:
        interface_txt = '(' + ', '.join(
            [type_ + ' ' + name for type_, name in args]) + ')'
    else:
        if binder.parent.is_class_template:
            prefix = binder.parent.qualified_name
        else:
            prefix = binder.parent.type.spelling
        interface_txt = '({} &self{}'.format(prefix,
                                             delimiter) + ', '.join(
            [type_ + ' ' + name for type_, name in args]) + ')'
        is_static = False

    # Function call
    is_void = False
    if binder.rtype.spelling == 'void':
        is_void = True
    args_txt = ', '.join([name for _, name in args])
    if is_static:
        fcall = qname
    else:
        fcall = 'self.{}'.format(binder.spelling)

    # Return type
    rtype = binder.rtype.spelling

    if is_void:
        func_txt = '{{ {}({}); '.format(fcall, args_txt)
    else:
        func_txt = '{{ {} rv = {}({}); '.format(rtype, fcall, args_txt)

    return_args = ', '.join([name for _, name in non_const_immutable_args])
    return_types = ', '.join([type_ for type_, _ in non_const_immutable_args])
    if is_void:
        if len(non_const_immutable_args) > 1:
            return_txt = 'return std::tuple<{}>({}); }}'.format(return_types,
                                                                return_args)
        else:
            return_txt = 'return ' + return_args + '; }'
    else:
        return_txt = 'return std::tuple<{}, {}>(rv, {}); }}'.format(rtype,
                                                                    return_types,
                                                                    return_args)

    # Binding text
    bind_txt = '[]' + interface_txt + func_txt + return_txt
    return bind_txt


def patch_src(filename, src):
    """
    Patches the source in place. If no patches are set for the filename this is
    a no-op.
    :param str filename: The file to patch excluding the extension
    :param list src: The source lines of the file before they're written
    """
    if filename not in Generator.patches:
        return

    line_start = len(SRC_PREFIX.split("\n"))

    for find, replace in Generator.patches[filename]:
        for i, line in enumerate(src[:]):
            if find in line:
                new_line = line.replace(find, replace)
                msg = "Patched file: {}".format(filename)
                logger.write(msg)
                print(msg)

                # Update the src line
                src[i] = new_line
