import argparse
import json
import os
import sys
from os.path import join, abspath, exists

# If running outside this folder we need to add this to the syspath
BINDER_ROOT = os.path.dirname(os.path.dirname(__file__))
if BINDER_ROOT not in sys.path:
    sys.path.append(BINDER_ROOT)

from binder.core import Generator


def find_include_path(name, path):
    """
    Attempt to find an include directory of a given header file.

    :param name: The header file to search for.
    :param path: The starting path.

    :return: The full path to the directory of the given header file.
    """
    for root, dirs, files in os.walk(path):
        if name in files:
            return root


def gen_includes(opencascade_include_path='../include/opencascade',
                 output_dir='.'):
    output_dir = abspath(output_dir)

    # Generate all_includes.h and output modules
    all_includes = []

    occt_mods = set()
    for fin in os.listdir(opencascade_include_path):
        if fin.endswith('.hxx'):
            all_includes.append(fin)
        if '_' in fin:
            mod = fin.split('_')[0]
        else:
            mod = fin.split('.')[0]
        occt_mods.add(mod)

    # OCCT modules
    occt_mods = list(occt_mods)
    occt_mods.sort(key=str.lower)
    with open(join(output_dir, 'all_modules.json'), 'w') as fout:
        json.dump(occt_mods, fout, indent=4)

    # Sort ignoring case
    all_includes.sort(key=str.lower)

    # all_includes.h
    with open(join(output_dir, 'all_includes.h'), 'w') as fout:
        fout.write("/*****************************************************/\n")
        fout.write("/* Do not edit! This file is automatically generated */\n")
        fout.write("/*****************************************************/\n")
        fout.write("#ifdef _WIN32\n")
        fout.write('    #include <Windows.h>\n')
        fout.write("#endif\n")

        fout.write("\n// OCCT\n")
        for header in all_includes:
            fout.write('#include <{}>\n'.format(header))

    return occt_mods


def main():
    parser = argparse.ArgumentParser()
    print('=' * 100)
    print("pyOCCT Binder")
    print('=' * 100)

    parser.add_argument(
        '-c',
        help='Path to config.txt',
        dest='config_path',
        default=join(BINDER_ROOT, 'generate', 'config.txt'))

    parser.add_argument(
        '-o',
        help='Path to pyOCCT',
        default='.',
        dest='pyocct_path')

    args = parser.parse_args()

    # Get the root directory of the conda environment
    conda_prefix = os.environ.get('CONDA_PREFIX')

    # Attempt to find include directories by searching for a known header file. Will likely
    # need to make this more robust.
    opencascade_include_path = find_include_path('Standard.hxx', conda_prefix)
    vtk_include_path = find_include_path('vtk_doubleconversion.h', conda_prefix)
    tbb_include_path = find_include_path('tbb.h', conda_prefix)
    tbb_include_path = os.path.split(tbb_include_path)[0]

    print('Include directories:')
    print('\tOpenCASCADE: {}'.format(opencascade_include_path))
    print('\tVTK: {}'.format(vtk_include_path))
    print('\tTBB: {}'.format(tbb_include_path))

    # TODO: Move this to pyOCCT. Add include directory for missing OCCT header files.
    missing_includes = abspath(join(BINDER_ROOT, 'generate', 'extra_includes'))

    clang_include_path = ''
    if sys.platform.startswith('linux'):
        clang_include_path = find_include_path('__stddef_max_align_t.h', conda_prefix)
        print('Found clangdev include directory: {}'.format(clang_include_path))

    extra_includes = [i for i in [vtk_include_path, tbb_include_path,
                                  clang_include_path, missing_includes] if i]

    if not opencascade_include_path or not exists(opencascade_include_path):
        print(f"ERROR: OpenCASCADE include path does not exist:"
              f"{opencascade_include_path}")
        sys.exit(1)

    if not exists(args.pyocct_path):
        print(f"ERROR: pyOCCT path is does not exist: "
              f"{args.pyocct_path}")
        sys.exit(1)

    if not exists(args.config_path):
        print(f"ERROR: binder config path is does not exist: "
              f"{args.config_path}")
        sys.exit(1)

    # TODO: Force using conda's clangdev includes. This may not be needed on other systems but was
    #  getting errors on linux.
    if sys.platform.startswith('linux') and not exists(clang_include_path):
        print(f"ERROR: libclang include path is does not exist:"
              f"{clang_include_path}")
        sys.exit(1)

    # TODO: Move this to the binder
    print('Collecting OpenCASCADE headers...')
    gen_dir = abspath(join(BINDER_ROOT, 'generate'))
    occt_mods = gen_includes(opencascade_include_path, gen_dir)

    gen = Generator(occt_mods, opencascade_include_path, *extra_includes)

    pyocct_inc = abspath(join(args.pyocct_path, 'inc'))
    pyocct_src = abspath(join(args.pyocct_path, 'src', 'occt'))

    if not exists(pyocct_inc):
        os.makedirs(pyocct_inc)

    if not exists(pyocct_src):
        os.makedirs(pyocct_src)

    print(f"Writing inc files to: {pyocct_inc}")
    print(f"Writing src files to: {pyocct_src}")

    # For debugging and dev
    gen.bind_enums = True
    gen.bind_functions = True
    gen.bind_classes = True
    gen.bind_typedefs = True
    gen.bind_class_templates = True

    gen.process_config(args.config_path)

    print('Generate common header file...')
    gen.generate_common_header(pyocct_inc)

    print('Parsing headers...')
    gen.parse(join(gen_dir, 'all_includes.h'))
    gen.dump_diagnostics()

    print('Traversing headers...')
    gen.traverse()

    print('Sorting binders...')
    gen.sort_binders()

    print('Building includes...')
    gen.build_includes()

    print('Building imports...')
    gen.build_imports()

    print('Checking circular imports...')
    gen.check_circular()

    print('Binding templates...')
    gen.bind_templates(pyocct_src)

    print('Binding...')
    gen.bind(pyocct_src)
    print('Done!')
    print('=' * 100)


if __name__ == '__main__':
    main()
