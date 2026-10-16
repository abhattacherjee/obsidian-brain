"""macOS Desktop transport proof; declarations and thread metadata are not proof.

Supported code: OpenAI rust-v0.162.0-alpha.17.2. The published release and the
bundled executable have identical bytes before LC_CODE_SIGNATURE. An app update
must be reviewed before these pins change. No frontend is inferred from a thread.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import struct
import subprocess
import sys
import time
import uuid

APP = Path('/Applications/ChatGPT.app')
DESKTOP = str(APP / 'Contents/MacOS/ChatGPT')
SERVER = str(APP / 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
ASAR = str(APP / 'Contents/Resources/app.asar')
PINS = {
    DESKTOP: ('239db1b91fbac28c04c96f19ec9ccfda4fb3c9912a2607b50ffc4591233b552a',
              '54ec7f8749a10262f4f06fbb66688c3a1f78137b'),
    SERVER: ('0d5ac7aae20c4eda6242428d5e218c55ba96d8763fda2a271088ff37104b9d28',
             '9968cf1ea4020a7730965d776bd02cdc2ce4544d'),
    ASAR: ('2f231a64e6dd30111bcd09e03c149cd06c8679b25914842772423251c4608ded', None),
}
FILE_SIZES = {DESKTOP: 71008, SERVER: 246046384, ASAR: 567770955}
# Native addons are outside ASAR; pin them too before observing a fresh app.
UNPACKED_PINS = {'/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/dist/native/darwin/permissions.node': ('ef66e12f1f573b46500bb85942ae22da38e6f10792bfd90f62be1c8b320898e5',
                                                                                                                                                                                       138704),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/node_modules/node-hid/prebuilds/HID-darwin-arm64/node-napi-v4.node': ('1caeef23d9b4e2e062a334115b5d930de9c71a725111752eea7242465ae450ab',
                                                                                                                                                                                                                      240608),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/node_modules/serialport/node_modules/@serialport/bindings-cpp/prebuilds/android-arm/node.napi.armv7.node': ('7947c3e850b753da5b602c776028aae372b4f7847dfbaf7c696f1ee743a7e1eb',
                                                                                                                                                                                                                                                            93432),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/node_modules/serialport/node_modules/@serialport/bindings-cpp/prebuilds/android-arm64/node.napi.armv8.node': ('bdfe8f469663f1adca8f910e669950dacdf4710182ba147d94beba47174b594f',
                                                                                                                                                                                                                                                              132552),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/node_modules/serialport/node_modules/@serialport/bindings-cpp/prebuilds/darwin-x64+arm64/node.napi.node': ('957390ba5f0bbd1b59ef2cf09378ee40a16a0f4440a55db43d48072f9e90914b',
                                                                                                                                                                                                                                                           324672),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/node_modules/serialport/node_modules/@serialport/bindings-cpp/prebuilds/linux-arm/node.napi.armv6.node': ('b10a83aca31ebf21b901affc61961a16924aa558ddf4b27ed2aa694ecb00f1e5',
                                                                                                                                                                                                                                                          117012),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/node_modules/serialport/node_modules/@serialport/bindings-cpp/prebuilds/linux-arm/node.napi.armv7.node': ('0f3fc12ed86fb8ea6460c7d2b612db997bd0bb6c8a6eb7492f93dcad376c7cea',
                                                                                                                                                                                                                                                          117024),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/node_modules/serialport/node_modules/@serialport/bindings-cpp/prebuilds/linux-arm64/node.napi.armv8.node': ('2341a58d2c3d99f827a411c65cb1c9c9f00a80b62e9bbbde58b2f29625fd56d9',
                                                                                                                                                                                                                                                            146744),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/node_modules/serialport/node_modules/@serialport/bindings-cpp/prebuilds/linux-x64/node.napi.glibc.node': ('c32a2a98bb31220544a2e866a9d2bee618b2fa8bdc735ffdabec19421c589960',
                                                                                                                                                                                                                                                          154928),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/node_modules/serialport/node_modules/@serialport/bindings-cpp/prebuilds/linux-x64/node.napi.musl.node': ('a9bad9a88cb0b67ad1807debce24510d581752c7aef3d9f2bcce30b119ac3009',
                                                                                                                                                                                                                                                         153776),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/node_modules/serialport/node_modules/@serialport/bindings-cpp/prebuilds/win32-ia32/node.napi.node': ('df91e6a6764481b58c95f07a7f81193ae4f99877d1e3da26a05f99664b2f2b23',
                                                                                                                                                                                                                                                     179200),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/@worklouder/device-kit-oai/node_modules/@worklouder/wl-device-kit/node_modules/serialport/node_modules/@serialport/bindings-cpp/prebuilds/win32-x64/node.napi.node': ('2d5d112e2e57054034fa9454e9c33dc306377455d989644f7d4c1b43027eb006',
                                                                                                                                                                                                                                                    229376),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/better-sqlite3/build/Release/better_sqlite3.node': ('c647620c46ab2232733f1a3f851894577584f0dfab7ad85b912d16fbc00a1b18',
                                                                                                                                  1882080),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/node-pty/build/Release/pty.node': ('9a8f27004ff16f46d0f1595a379c86a69b424a9cbf2ae7bdbb8a956c2b4cf174',
                                                                                                                 102368),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/node-pty/build/Release/spawn-helper': ('1c6622bde70e7080dbcd55005d3857390b0591cef996fd1d60f4edb204dc1d1c',
                                                                                                                     68448),
 '/Applications/ChatGPT.app/Contents/Resources/app.asar.unpacked/node_modules/objc-js/prebuilds/darwin-arm64/node.napi.armv8.node': ('d79a5faf8db5b74f1e73a1c6e9e693247fa9c03cb80364c7a5bd915ad31f3f09',
                                                                                                                                     642064)}
for _path, (_sha, _size) in UNPACKED_PINS.items():
    PINS[_path] = (_sha, None)
    FILE_SIZES[_path] = _size

POLICY_TEXT = b'allow_remote_control = false\n'
POLICY_VALUE = base64.b64encode(POLICY_TEXT).decode('ascii')
PROFILE_IDENTIFIER = 'org.obsidian-brain.desktop-isolation'


def forced_policy():
    """The OS forced layer overrides cloud/system scalars in the pinned loader."""
    cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    pointer = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = pointer
    cf.CFPreferencesAppValueIsForced.argtypes = [pointer, pointer]
    cf.CFPreferencesAppValueIsForced.restype = ctypes.c_bool
    cf.CFPreferencesCopyAppValue.argtypes = [pointer, pointer]
    cf.CFPreferencesCopyAppValue.restype = pointer
    cf.CFGetTypeID.argtypes = [pointer]
    cf.CFGetTypeID.restype = ctypes.c_ulong
    cf.CFStringGetTypeID.restype = ctypes.c_ulong
    cf.CFStringGetCString.argtypes = [pointer, pointer, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [pointer]
    owned = []
    try:
        key = cf.CFStringCreateWithCString(None, b'requirements_toml_base64', 0x08000100)
        domain = cf.CFStringCreateWithCString(None, b'com.openai.codex', 0x08000100)
        owned.extend([key, domain])
        if not key or not domain or not cf.CFPreferencesAppValueIsForced(key, domain):
            raise ValueError('Desktop remote control is not disabled by forced OS policy')
        value = cf.CFPreferencesCopyAppValue(key, domain)
        owned.append(value)
        if not value or cf.CFGetTypeID(value) != cf.CFStringGetTypeID():
            raise ValueError('Forced Desktop policy is not a string')
        buf = ctypes.create_string_buffer(4096)
        if not cf.CFStringGetCString(value, buf, len(buf), 0x08000100):
            raise ValueError('Forced Desktop policy exceeds its bound')
        if buf.value.decode('utf-8') != POLICY_VALUE:
            raise ValueError('Forced Desktop policy does not match the reviewed profile')
        return hashlib.sha256(POLICY_TEXT).hexdigest()
    finally:
        for value in reversed(owned):
            if value:
                cf.CFRelease(value)


def _file_identity(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Desktop proof source is not a regular single-link file')
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _pin_files():
    result = {}
    for path, (expected, _) in PINS.items():
        before = _file_identity(path)
        if before[2] != FILE_SIZES[path]:
            raise ValueError('Installed Desktop code size does not match its supported pin')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            digest = hashlib.sha256()
            remaining = before[2]
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError('Desktop proof source was truncated')
                digest.update(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ValueError('Desktop proof source exceeded its bound')
            opened = os.fstat(descriptor)
            if [opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns] != before:
                raise ValueError('Desktop proof source changed while hashing')
        finally:
            os.close(descriptor)
        if digest.hexdigest() != expected or _file_identity(path) != before:
            raise ValueError('Installed Desktop code does not match its supported pin')
        result[path] = before
    return result


def _receipt_path(uid):
    return Path(pwd.getpwuid(uid).pw_dir) / 'Library/Application Support/obsidian-brain-desktop/binding.json'


def _private_directory(path, uid):
    """Parents may be user-owned, but cannot redirect a privileged write."""
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, uid} or info.st_mode & 0o022:
            raise ValueError('Desktop proof parent is unsafe')
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Desktop proof directory must be private and owned')
    return info.st_dev, info.st_ino


def _read_receipt(uid):
    path = _receipt_path(uid)
    directory = _private_directory(path.parent, uid)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o444 or not 0 < info.st_size <= 32768):
            raise ValueError('Desktop attestation is not root-owned read-only metadata')
        raw = os.read(descriptor, 32769)
        after = os.fstat(descriptor)
        if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
             info.st_uid, info.st_mode, info.st_nlink)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                    after.st_uid, after.st_mode, after.st_nlink) or len(raw) != info.st_size):
            raise ValueError('Desktop attestation changed while reading')
    finally:
        os.close(descriptor)
    if _private_directory(path.parent, uid) != directory:
        raise ValueError('Desktop attestation directory changed')
    current = path.lstat()
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise ValueError('Desktop attestation was replaced')
    return json.loads(raw)


def verified_desktop_ancestor(start_pid=None):
    """Fail closed before payload access; the nearest native owner decides."""
    if sys.platform != 'darwin':
        return False
    deadline = time.monotonic() + 1.0
    try:
        reader = MacReader()
        uid = os.getuid()
        receipt = _read_receipt(uid)
        if (receipt.get('schema') != 1 or receipt.get('uid') != uid
                or receipt.get('boot') != _boot_id() or receipt.get('policy') != forced_policy()):
            return False
        pid, seen = start_pid or os.getppid(), set()
        for _ in range(32):
            if pid <= 1 or pid in seen or time.monotonic() >= deadline:
                return False
            seen.add(pid)
            identity = reader.identity(pid)
            if Path(identity[4]).name in {'codex', 'codex-cli'}:
                # A nearer CLI/proxy never inherits the outer Desktop's identity.
                proof = collect_transport(reader, pid)
                break
            pid = identity[1]
        else:
            return False
        for name in ('server', 'desktop', 'pairs', 'signatures'):
            if json.loads(json.dumps(proof[name])) != receipt.get(name):
                return False
        if (type(receipt['observed_before_us']) is not int
                or not 0 < receipt['observed_before_us'] < proof['desktop'][3] < proof['server'][3]):
            return False
        if receipt.get('files') != {path: _file_identity(path) for path in PINS}:
            return False
        if receipt.get('observer_sha256') != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
            return False
        # Every hook checks live signatures/pairs and current forced policy again.
        return (forced_policy() == receipt['policy'] and reader.identity(pid) == proof['server']
                and reader.identity(proof['desktop'][0]) == proof['desktop']
                and receipt['files'] == {path: _file_identity(path) for path in PINS}
                and time.monotonic() < deadline)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return False


def _boot_id():
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    buf, size = ctypes.create_string_buffer(80), ctypes.c_size_t(80)
    if lib.sysctlbyname(b'kern.bootsessionuuid', buf, ctypes.byref(size), None, 0):
        raise OSError('Boot identity is unavailable')
    return str(uuid.UUID(buf.value.decode('ascii')))


def profile_payload():
    """A removable app-only system profile; it has no enrollment payload."""
    return {
        'PayloadType': 'Configuration', 'PayloadVersion': 1,
        'PayloadIdentifier': PROFILE_IDENTIFIER,
        'PayloadUUID': str(uuid.uuid5(uuid.NAMESPACE_URL, PROFILE_IDENTIFIER)),
        'PayloadDisplayName': 'Obsidian Brain: isolated Codex Desktop',
        'PayloadDescription': 'Disable Codex device remote control for verified local Desktop hooks.',
        'PayloadScope': 'System', 'PayloadRemovalDisallowed': False,
        'PayloadContent': [{
            'PayloadType': 'com.apple.ManagedClient.preferences', 'PayloadVersion': 1,
            'PayloadIdentifier': PROFILE_IDENTIFIER + '.preferences',
            'PayloadUUID': str(uuid.uuid5(uuid.NAMESPACE_URL, PROFILE_IDENTIFIER + '.preferences')),
            'PayloadContent': {'com.openai.codex': {'Forced': [{
                'mcx_preference_settings': {'requirements_toml_base64': POLICY_VALUE},
            }]}},
        }],
    }


def _observer_is_protected():
    if os.geteuid() != 0 or not sys.flags.isolated:
        raise ValueError('Use the reviewed root-staged observer with Python isolated mode')
    path = Path(__file__)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or info.st_mode & 0o222):
        raise ValueError('Observer source must be root-owned and read-only')
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Observer staging directory is not protected')
    source, null = os.fstat(0), os.stat('/dev/null')
    if not stat.S_ISCHR(source.st_mode) or source.st_rdev != null.st_rdev:
        raise ValueError('Run the observer with stdin redirected from /dev/null')


def _user_policy(uid):
    """Probe the console user's OS policy in a fresh, privilege-dropped child."""
    import inspect
    account = pwd.getpwuid(uid)
    source = ('import ctypes, hashlib\nPOLICY_VALUE=' + repr(POLICY_VALUE)
              + '\nPOLICY_TEXT=' + repr(POLICY_TEXT) + '\n'
              + inspect.getsource(forced_policy) + '\nprint(forced_policy())\n')

    def drop_privileges():
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(uid)

    try:
        result = subprocess.run(['/usr/bin/python3', '-I', '-c', source],
                                env={'PATH': '/usr/bin:/bin', 'HOME': account.pw_dir},
                                preexec_fn=drop_privileges, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired:
        raise ValueError('Console user OS policy probe exceeded its deadline') from None
    expected = hashlib.sha256(POLICY_TEXT).hexdigest()
    if result.returncode or result.stdout.strip() != expected:
        raise ValueError('Console user does not have the reviewed forced OS policy')
    return expected


def _write_receipt(uid, receipt):
    path = _receipt_path(uid)
    # Walk from / using directory descriptors. A user-controlled ancestor cannot
    # redirect a privileged mkdir/open through a swapped symlink.
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = path.parent.parts[1:]
        for index, component in enumerate(parts):
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                if index != len(parts) - 1:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=directory)
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.fchown(child, uid, pwd.getpwuid(uid).pw_gid)
            info = os.fstat(child)
            if info.st_uid not in {0, uid} or info.st_mode & 0o022:
                os.close(child)
                raise ValueError('Desktop proof parent is unsafe')
            os.close(directory)
            directory = child
        info = os.fstat(directory)
        if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Desktop proof directory is not private')
        before = (info.st_dev, info.st_ino)
    except BaseException:
        os.close(directory)
        raise
    name = '.binding-' + uuid.uuid4().hex
    descriptor = None
    try:
        try:
            old = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            old = None
        if old is not None and (not stat.S_ISREG(old.st_mode) or old.st_uid != 0
                                or old.st_nlink != 1 or stat.S_IMODE(old.st_mode) != 0o444):
            raise ValueError('Existing Desktop attestation is unsafe')
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o400, dir_fd=directory)
        raw = (json.dumps(receipt, sort_keys=True, separators=(',', ':')) + '\n').encode()
        if len(raw) > 32768:
            raise ValueError('Desktop attestation exceeds its bound')
        with os.fdopen(descriptor, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
        if _private_directory(path.parent, uid) != before:
            raise ValueError('Desktop attestation directory changed')
        os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        if _private_directory(path.parent, uid) != before:
            raise ValueError('Desktop attestation directory changed')
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)
    return path


def observe_new_desktop(seconds):
    """Finite foreground observation; never launch, restart or signal an app."""
    _observer_is_protected()
    if not 1 <= seconds <= 600:
        raise ValueError('Observer deadline must be between 1 and 600 seconds')
    uid = os.stat('/dev/console').st_uid
    if uid <= 0:
        raise ValueError('No ordinary console user is available')
    # Both device/root and user views must be OS-forced, before either process.
    policy = forced_policy()
    if _user_policy(uid) != policy:
        raise ValueError('Forced OS policy views differ')
    files, boot = _pin_files(), _boot_id()
    observed_before = time.time_ns() // 1000
    deadline, reader = time.monotonic() + seconds, MacReader()
    print(json.dumps({'status': 'awaiting-new-desktop', 'deadline_seconds': seconds,
                      'uid': uid, 'observed_before_us': observed_before}), flush=True)
    while time.monotonic() < deadline:
        if forced_policy() != policy or _user_policy(uid) != policy or _boot_id() != boot:
            raise ValueError('Protected policy or boot changed')
        pids = (ctypes.c_int * 32768)()
        count = reader.lib.proc_listpids(4, uid, pids, ctypes.sizeof(pids))
        if count <= 0 or count >= ctypes.sizeof(pids) or count % 4:
            raise OSError('Owned process inventory is unavailable')
        for pid in pids[:count // 4]:
            if pid <= 1:
                continue
            try:
                identity = reader.identity(pid)
            except OSError:
                continue  # An unrelated process can exit during the inventory.
            if identity[4] != SERVER or identity[3] <= observed_before:
                continue
            try:
                proof = collect_transport(reader, pid)
            except (OSError, ValueError):
                continue
            if proof['desktop'][3] <= observed_before or proof['server'][2] != uid:
                continue
            for item in (proof['desktop'], proof['server']):
                reader.signature(item[0], check_resources=True)
            if (files != {path: _file_identity(path) for path in PINS}
                    or forced_policy() != policy or _user_policy(uid) != policy):
                raise ValueError('Desktop code or forced policy changed')
            if time.monotonic() >= deadline:
                raise ValueError('Observer deadline elapsed before attestation')
            receipt = {'schema': 1, 'uid': uid, 'boot': boot, 'policy': policy,
                       'observed_before_us': observed_before, 'files': files,
                       'observer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), **proof}
            path = _write_receipt(uid, receipt)
            print(json.dumps({'status': 'attested', 'path': str(path),
                              'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}), flush=True)
            return 0
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    raise ValueError('No newly launched pinned Desktop stdio transport was observed')


def main():
    import argparse
    import plistlib
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--profile-output', type=Path)
    choice.add_argument('--watch', type=int, metavar='SECONDS')
    args = parser.parse_args()
    try:
        if args.profile_output is not None:
            if os.geteuid() == 0:
                raise ValueError('Generate the reviewable profile without privilege')
            directory = args.profile_output.parent.lstat()
            if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                    or stat.S_IMODE(directory.st_mode) != 0o700
                    or args.profile_output.suffix != '.mobileconfig'):
                raise ValueError('Use a private owned directory and a .mobileconfig filename')
            descriptor = os.open(args.profile_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(plistlib.dumps(profile_payload(), sort_keys=True))
            return 0
        return observe_new_desktop(args.watch)
    except (OSError, ValueError, TypeError, KeyError) as error:
        print('[obsidian-brain] Desktop proof refused: ' + str(error), file=sys.stderr)
        return 1


def _stdio_arguments(argv):
    """Recognize only the reviewed app-server grammar, never prompt text."""
    args = list(argv[1:])
    found = False
    while args:
        arg = args.pop(0)
        if arg in {'-c', '--config'}:
            if not args or '=' not in args.pop(0):
                return False
        elif arg == 'app-server' and not found:
            found = True
        elif arg == '--analytics-default-enabled' and found:
            continue
        elif arg == '--listen' and found:
            if not args or args.pop(0) != 'stdio://':
                return False
        else:
            return False
    return found


def collect_transport(reader, owner_pid):
    """Prove the live local transport. This alone does not admit a hook."""
    server = reader.identity(owner_pid)
    desktop = reader.identity(server[1])
    if (server[4] != SERVER or desktop[4] != DESKTOP or server[2] != desktop[2]
            or desktop[3] >= server[3] or not _stdio_arguments(reader.argv(owner_pid))):
        raise ValueError('Desktop process or transport is unsupported')
    signatures = [reader.signature(desktop[0]), reader.signature(server[0])]
    if signatures != [PINS[DESKTOP][1], PINS[SERVER][1]]:
        raise ValueError('Running Desktop code is not pinned')
    server_sockets, app_sockets = reader.sockets(server[0]), reader.sockets(desktop[0])
    if any(item['listener'] for item in server_sockets):
        raise ValueError('App-server has a competing listener')
    pairs = []
    for fd in (0, 1):
        endpoints = [item for item in server_sockets if item['fd'] == fd]
        if len(endpoints) != 1:
            raise ValueError('Desktop stdio endpoint is unavailable')
        item = endpoints[0]
        peers = [p for p in app_sockets if p['handle'] == item['peer'] and p['peer'] == item['handle']]
        if (len(peers) != 1 or not item['anonymous'] or item['shared']
                or not peers[0]['anonymous'] or peers[0]['shared']):
            raise ValueError('Desktop stdio endpoint is not private and reciprocal')
        pairs.append((fd, peers[0]['fd'], item['handle'], item['peer']))
    if len({item[2] for item in pairs}) != 2 or len({item[3] for item in pairs}) != 2:
        raise ValueError('Desktop stdio endpoints are duplicated')
    if reader.identity(server[0]) != server or reader.identity(desktop[0]) != desktop:
        raise ValueError('Desktop process identity changed')
    # Re-read both sides; closing/replacing an FD while checking is a refusal.
    if reader.sockets(server[0]) != server_sockets or reader.sockets(desktop[0]) != app_sockets:
        raise ValueError('Desktop transport changed')
    return {'server': server, 'desktop': desktop, 'pairs': pairs, 'signatures': signatures}


class MacReader:
    """Read only process identities, argument boundaries and socket metadata."""

    def __init__(self):
        if sys.platform != 'darwin':
            raise OSError('Desktop binding is supported only on macOS')
        self.lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        self.lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_pidfdinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]

    def identity(self, pid):
        buf = ctypes.create_string_buffer(136)
        if self.lib.proc_pidinfo(pid, 3, 0, buf, len(buf)) != 136:
            raise OSError('Process identity is unavailable')
        actual, parent, uid = struct.unpack_from('III', buf, 12)
        seconds, micros = struct.unpack_from('QQ', buf, 120)
        path = ctypes.create_string_buffer(4096)
        if actual != pid or self.lib.proc_pidpath(pid, path, len(path)) <= 0:
            raise OSError('Process executable is unavailable')
        return pid, parent, uid, seconds * 1_000_000 + micros, os.fsdecode(path.value)

    def argv(self, pid):
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t()
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not 4 < size.value <= 1_000_000:
            raise OSError('Process argument size is unavailable')
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
            raise OSError('Process arguments are unavailable')
        raw = buf.raw[:size.value]
        count = struct.unpack_from('i', raw)[0]
        if not 0 < count <= 4096:
            raise ValueError('Process argument count is invalid')
        offset = raw.index(b'\0', 4) + 1
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        argv = []
        for _ in range(count):
            end = raw.index(b'\0', offset)
            argv.append(os.fsdecode(raw[offset:end]))
            offset = end + 1
        return argv

    def sockets(self, pid):
        buf = ctypes.create_string_buffer(131072)
        count = self.lib.proc_pidinfo(pid, 1, 0, buf, len(buf))
        if count <= 0 or count >= len(buf) or count % 8:
            raise OSError('Process descriptors are unavailable')
        result = []
        for fd, kind in struct.iter_unpack('iI', buf.raw[:count]):
            if kind != 2:
                continue
            info = ctypes.create_string_buffer(792)
            if self.lib.proc_pidfdinfo(pid, fd, 3, info, len(info)) != 792:
                raise OSError('Process socket identity is unavailable')
            family, socket_kind = struct.unpack_from('i', info, 184)[0], struct.unpack_from('i', info, 256)[0]
            options, qlimit = struct.unpack_from('h', info, 188)[0], struct.unpack_from('h', info, 198)[0]
            if family == 1 and socket_kind == 3:
                # Darwin SDK socket_fdinfo: psi at24, soi_so160, pri_un264.
                # socketpair endpoints have no sockaddr_un path on either side.
                anonymous = not any(info.raw[282:535]) and not any(info.raw[537:790])
                handle, peer = struct.unpack_from('Q', info, 160)[0], struct.unpack_from('Q', info, 264)[0]
            else:
                anonymous, handle, peer = False, 0, 0
            result.append({'fd': fd, 'handle': handle, 'peer': peer,
                           'shared': bool(struct.unpack_from('I', info, 4)[0] & 1),
                           'anonymous': anonymous, 'listener': bool(options & 2 or qlimit)})
        return result

    def signature(self, pid, *, check_resources=False):
        api = AppleSecurity()
        return api.signature(pid, check_resources=check_resources)


class AppleSecurity:
    """Use Apple's running-code identity, not a subprocess or disk name guess."""

    def __init__(self):
        self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        pointer = ctypes.c_void_p
        declarations = {
            'CFNumberCreate': ([pointer, ctypes.c_int, pointer], pointer),
            'CFDictionaryCreate': ([pointer, pointer, pointer, ctypes.c_long, pointer, pointer], pointer),
            'CFDictionaryGetValue': ([pointer, pointer], pointer),
            'CFDataGetLength': ([pointer], ctypes.c_long),
            'CFDataGetBytePtr': ([pointer], pointer),
            'CFRelease': ([pointer], None),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.cf, name)
            function.argtypes, function.restype = arguments, result
        for name, arguments in {
            'SecCodeCopyGuestWithAttributes': [pointer, pointer, ctypes.c_uint32, pointer],
            'SecCodeCheckValidity': [pointer, ctypes.c_uint32, pointer],
            'SecCodeCopySigningInformation': [pointer, ctypes.c_uint32, pointer],
            'SecCodeCopyStaticCode': [pointer, ctypes.c_uint32, pointer],
            'SecStaticCodeCheckValidity': [pointer, ctypes.c_uint32, pointer],
        }.items():
            getattr(self.sec, name).argtypes = arguments
            getattr(self.sec, name).restype = ctypes.c_int32

    def signature(self, pid, *, check_resources=False):
        pointer = ctypes.c_void_p
        number = ctypes.c_int(pid)
        owned = []
        try:
            pid_value = self.cf.CFNumberCreate(None, 9, ctypes.byref(number))
            owned.append(pid_value)
            key = pointer.in_dll(self.sec, 'kSecGuestAttributePid')
            keys, values = (pointer * 1)(key.value), (pointer * 1)(pid_value)
            attrs = self.cf.CFDictionaryCreate(None, keys, values, 1, None, None)
            owned.append(attrs)
            code = pointer()
            if self.sec.SecCodeCopyGuestWithAttributes(None, attrs, 0, ctypes.byref(code)):
                raise OSError('Running code identity is unavailable')
            owned.append(code.value)
            if self.sec.SecCodeCheckValidity(code, 16, None):
                raise ValueError('Running code signature is invalid')
            if check_resources:
                static = pointer()
                if self.sec.SecCodeCopyStaticCode(code, 0, ctypes.byref(static)):
                    raise OSError('Static code identity is unavailable')
                owned.append(static.value)
                if self.sec.SecStaticCodeCheckValidity(static, 16, None):
                    raise ValueError('Signed application resources are invalid')
            info = pointer()
            if self.sec.SecCodeCopySigningInformation(code, 2, ctypes.byref(info)):
                raise OSError('Running code signing information is unavailable')
            owned.append(info.value)
            key = pointer.in_dll(self.sec, 'kSecCodeInfoUnique')
            data = self.cf.CFDictionaryGetValue(info, key)
            if not data or self.cf.CFDataGetLength(data) != 20:
                raise ValueError('Running code hash is unavailable')
            return ctypes.string_at(self.cf.CFDataGetBytePtr(data), 20).hex()
        finally:
            for value in reversed(owned):
                if value:
                    self.cf.CFRelease(value)


if __name__ == '__main__':
    raise SystemExit(main())
