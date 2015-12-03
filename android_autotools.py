import argparse
import subprocess
import shutil
import tempfile
import os.path
import os
import glob
import json
import copy
import multiprocessing
import datetime

from collections import OrderedDict
from subprocess import PIPE, Popen

fpath = os.path.dirname(os.path.realpath(__file__))

with open(os.path.join(fpath, "data.json")) as f:
    config = json.load(f, object_pairs_hook=OrderedDict)

def abi_to_arch(abi):
    for arch, o in config['archs'].items():
        if abi in o['abis']:
            return arch
    raise Exception("Unknown ABI: '%s'" % abi)

def abis_for_arch(arch):
    return tuple(config['archs'][arch]['abis'])

def all_abis():
    x = []
    for o in config['archs'].values():
        x += list(o['abis'])
    return tuple(x)

def log_tag(tag, *args):
    print("{} [{}] {}".format(str(datetime.datetime.now()), tag, " ".join(args)))

class SharedLibrary:
    def __init__(self, toolchain, src_dir, name, lib_path, **kwargs):
        self.toolchain = toolchain
        self.name = name
        self.path = lib_path
        self.src_dir = src_dir

        toolchain.cpp = True if kwargs.get('cpp', None) == True else False

    def _configure_props(self):
        return ['--disable-static', '--enable-shared']

    def _type_str(self):
        return "shared library"

    def build(self, *args, inject=None):
        t = self.toolchain
        s = self.src_dir
        t.make_distclean(s)

        log_tag(t.abi, "%s: ./configure" % self.name)
        t.configure(s, *(self._configure_props() + list(args)))

        if inject:
            log_tag(t.abi, "%s: injecting header content" % self.name)
            t.inject(s, inject)

        log_tag(t.abi, "%s: make" % self.name)
        t.make(s)

        log_tag(t.abi, "%s: make install" % self.name)
        t.make_install(s)

        log_tag(t.abi, "%s -> %s/%s (%s)" % (self.name, self.path, t.abi, self._type_str()))
        t.install_lib(self.name, self.path)

class StaticLibrary(SharedLibrary):
    def _configure_props(self):
        return ['--disable-shared', '--enable-static']

    def _type_str(self):
        return "static library"

class Toolchain:
    def __init__(self, path, arch, abi):
        self.arch = arch
        self.abi = abi
        self.path = path
        self.prefix = tempfile.TemporaryDirectory()

        # Toggled by external entities
        self.cpp = False

    def get_toolchain(self):
        return os.path.join(self.path, 'bin')

    def get_sysroot(self):
        return os.path.join(self.path, 'sysroot')

    def get_host(self):
        return os.path.basename(glob.glob(os.path.join(
                self.get_toolchain(), '*-gcc'))[0].rsplit('-', 1)[0])

    def get_env(self):
        host = self.get_host()
        sysroot = self.get_sysroot()

        o = os.environ.copy()
        o["PATH"] = "%s:%s" % (self.get_toolchain(), os.environ['PATH'])

        o["CPP"] = "%s-cpp" % host
        o["AR"] = "%s-ar" % host
        o["NM"] = "%s-nm" % host
        o["CC"] = "%s-gcc" % host
        o["CXX"] = "%s-g++" % host
        o["LD"] = "%s-ld" % host
        o["RANLIB"] = "%s-ranlib" % host
        o["STRIP"] = "%s-strip" % host
        o["LIBTOOL"] = "glibtool"

        o["PKG_CONFIG_PATH"] = os.path.join(self.prefix.name, 'lib', 'pkgconfig')

        cflags = copy.copy(config['cflags'])
        ldflags = copy.copy(config['ldflags'])
        abiflags = config['archs'][self.arch]['abis'][self.abi]

        if 'cflags' in abiflags:
            cflags += abiflags['cflags']

        if 'ldflags' in abiflags:
            ldflags += abiflags['ldflags']

        flags = '--sysroot=%s' % sysroot
        cflags.append(flags)
        ldflags.append('-L%s/lib -L%s/usr/lib -lm' % (self.prefix.name, sysroot))

        if self.cpp:
            cxxflags = copy.copy(config['cxxflags'])

            # Required for cstdint etc
            cpp_includes = glob.glob(os.path.join(self.path, 'include', 'gabi++', 'include'))[0]
            ldflags.append('-lstlport_shared')

            if 'cxxflags' in abiflags:
                cxxflags += abiflags['cxxflags']

            cxxflags += [
                flags,
                '-I%s' % cpp_includes,
                '-fexceptions',
                '-frtti'
            ]

            o['CXXFLAGS'] = " ".join(cxxflags)

        o['CFLAGS'] = " ".join(cflags)
        o['LDFLAGS'] = " ".join(ldflags)

        return o

    def configure(self, src_dir, *args):
        toolchain = self.get_toolchain()
        sysroot = self.get_sysroot()
        host = self.get_host()
        env = self.get_env()

        p = Popen(['./configure',
                    '--host', host,
                    '--prefix', self.prefix.name]
                    + list(args),
                    cwd=src_dir, env=env, stdout=PIPE, stderr=PIPE)
        out, err = p.communicate()

        if p.returncode != 0:
            raise IOError(err.decode())

    def make(self, src_dir):
        j = str(multiprocessing.cpu_count())

        p = Popen(['make', '-j', j],
            cwd=src_dir, env=self.get_env(), stdout=PIPE, stderr=PIPE)
        out, err = p.communicate()

        if p.returncode != 0:
            raise IOError(err.decode())

    def make_install(self, src_dir):
        p = Popen(['make', 'install'], cwd=src_dir, env=self.get_env(),
             stdout=PIPE, stderr=PIPE)
        out, err = p.communicate()

        if p.returncode != 0:
            raise IOError(err.decode())

    def make_distclean(self, src_dir):
        p = Popen(['make', 'distclean'], cwd=src_dir, env=self.get_env(),
            stdout=PIPE, stderr=PIPE)
        p.wait()
        # We don't care if this fails.

    def install_lib(self, libname, libdir):
        libdir = os.path.join(os.path.abspath(libdir), self.abi)
        os.makedirs(libdir, exist_ok=True)

        src = os.path.join(self.prefix.name, 'lib', libname)
        dest = os.path.join(libdir, libname)

        shutil.copyfile(src, dest)

    def install_stlport(self, libdir):
        libdir = os.path.join(os.path.abspath(libdir), self.abi)
        os.makedirs(libdir, exist_ok=True)

        libname = 'libstlport_shared.so'
        src = os.path.join(self.path, self.get_host(), 'lib', libname)
        dest = os.path.join(libdir, libname)

        shutil.copyfile(src, dest)

    def inject(self, src_dir, code):
        with open(os.path.join(src_dir, 'config.h'), 'a') as f:
            f.write('\n' + code.strip() + '\n')

class SickeningNightmare:
    def add_toolchain(self, arch):
        cmd = os.path.join(self.ndk_path,
            'build', 'tools', 'make-standalone-toolchain.sh')

        path = os.path.join(self.toolchain_dir.name, arch)
        log_tag(arch, "Building toolchain…")

        p = Popen(['sh', cmd,
            '--install-dir=%s' % path,
            '--stl=%s' % self.stl,
            '--arch=%s' % arch],
            stdout=PIPE, stderr=PIPE)

        out, err = p.communicate()
        if p.returncode != 0:
            raise IOError(out.decode())

        log_tag(arch, out.decode().split('\n')[0].strip())

        abis = abis_for_arch(arch)
        log_tag(arch, "Supported ABIs: %s" % ", ".join(abis))

        for abi in abis:
            self.toolchains[abi] = Toolchain(path, arch, abi)

    def __init__(self, ndk_path, lib_dir, archs=list(config['archs'].keys()),
                platform='android-14', stl='stlport'):
        if not os.path.isdir(ndk_path):
            raise Exception("ndk_path must be to the NDK directory")

        self.ndk_path = ndk_path
        self.lib_dir = lib_dir
        self.stl = stl
        self.toolchains = OrderedDict()
        self.toolchain_dir = tempfile.TemporaryDirectory()

        for arch in archs:
            self.add_toolchain(arch)

    def build(self, src_dir, libname, *args, inject=None, abis=None, **kwargs):
        if abis is None:
            abis = self.toolchains.keys()

        for abi in abis:
            toolchain = self.toolchains[abi]
            if libname.endswith('.a'):
                StaticLibrary(toolchain, src_dir, libname, self.lib_dir, **kwargs).build(*args, inject=inject)
            elif libname.endswith('.so'):
                SharedLibrary(toolchain, src_dir, libname, self.lib_dir, **kwargs).build(*args, inject=inject)

    def install_stlport(self, abis=None):
        if abis is None:
            abis = self.toolchains.keys()
        for abi in abis:
            toolchain = self.toolchains[abi]

            log_tag(abi, "%s -> %s/%s (%s)" % ('stlport_shared.so', self.lib_dir, abi, "shared library"))
            toolchain.install_stlport(self.lib_dir)

class BuildSet:
    def __init__(self, *args, **kwargs):
        self.nightmare = SickeningNightmare(*args, **kwargs)
        self.tasks = []
        self.cpp = False

    def add(self, *args, **kwargs):
        self.tasks.append({
            "args": args,
            "kwargs": kwargs
        })

    def run(self):
        for task in self.tasks:
            if task['kwargs'].get('cpp', None):
                self.cpp = True
            self.nightmare.build(*task['args'], **task['kwargs'])
        if self.cpp:
            self.nightmare.install_stlport()
