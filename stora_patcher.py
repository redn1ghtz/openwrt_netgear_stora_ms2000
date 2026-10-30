#!/usr/bin/env python3
"""Offline OpenWrt 25.12.6 Stora patcher: GPIO poweroff + prompted MAC only.
Requires Linux squashfs-tools. Never flashes the device.
"""
import argparse, hashlib, io, json, os, pathlib, re, shutil, struct, subprocess, sys, tarfile, tempfile, zlib

EXPECTED = '491827739fb920663e6ea5f772981f6bc861211f3c1d60f565ba7b8d21c066be'
MAGIC = b'\xd0\x0d\xfe\xed'
BASE = 'sysupgrade-netgear_stora/'
ROOTFS_CAPACITY = 25 * 129024  # confirmed ubi0:rootfs capacity on device
KERNEL_CAPACITY = 25 * 129024


def run(*cmd):
    subprocess.run([str(x) for x in cmd], check=True)


def extract_fwtool_metadata(image):
    """Validate original OpenWrt fwtool trailer and return its JSON payload."""
    if len(image) < 24 or image[-16:-12] != b'FWx0':
        raise ValueError('Original image has no fwtool metadata trailer')
    crc, = struct.unpack_from('>I', image, len(image) - 12)
    typ = image[-8]
    size, = struct.unpack_from('>I', image, len(image) - 4)
    if typ != 1 or image[-7:-4] != b'\0\0\0' or size < 24 or size > 30 * 1024:
        raise ValueError('Unsupported fwtool trailer')
    if (zlib.crc32(image[:-16]) ^ 0xffffffff) != crc:
        raise ValueError('Original fwtool CRC mismatch')
    chunk = image[-size:-16]
    if chunk[:8] != bytes(8):
        raise ValueError('Unexpected fwtool metadata header')
    meta = chunk[8:]
    info = json.loads(meta)
    if 'netgear,stora' not in info.get('supported_devices', []):
        raise ValueError('Wrong metadata device')
    return meta


def append_fwtool_metadata(path, meta):
    """Recreate fwtool -I metadata including CRC of the modified TAR."""
    with path.open('ab') as out:
        out.write(bytes(8))
        out.write(meta)
        out.flush()
    with path.open('rb') as f:
        crc = zlib.crc32(f.read()) ^ 0xffffffff
    trailer = b'FWx0' + struct.pack('>I', crc) + bytes([1, 0, 0, 0]) + struct.pack('>I', len(meta) + 24)
    with path.open('ab') as out:
        out.write(trailer)
    if extract_fwtool_metadata(path.read_bytes()) != meta:
        raise ValueError('Generated fwtool metadata validation failed')


def patch_kernel(blob):
    if blob[:4] != b'\x27\x05\x19\x56' or len(blob) < 64:
        raise ValueError('Not a legacy U-Boot uImage')
    if struct.unpack_from('>I', blob, 12)[0] != len(blob) - 64:
        raise ValueError('uImage payload length mismatch')
    if zlib.crc32(blob[64:]) != struct.unpack_from('>I', blob, 24)[0]:
        raise ValueError('uImage data CRC mismatch')
    header = bytearray(blob[:64]); struct.pack_into('>I', header, 4, 0)
    if zlib.crc32(header) != struct.unpack_from('>I', blob, 4)[0]:
        raise ValueError('uImage header CRC mismatch')

    fdtpos = -1
    start = 64
    while True:
        p = blob.find(MAGIC, start)
        if p < 0:
            raise ValueError('Embedded FDT not found')
        if p + 40 <= len(blob):
            size = struct.unpack_from('>I', blob, p + 4)[0]
            if 40 <= size <= len(blob)-p and struct.unpack_from('>I', blob, p + 20)[0] == 17:
                fdtpos = p; break
        start = p + 4
    size = struct.unpack_from('>I', blob, fdtpos+4)[0]
    fdt = bytearray(blob[fdtpos:fdtpos+size])
    off_struct, off_strings = struct.unpack_from('>II', fdt, 8)
    size_strings, size_struct = struct.unpack_from('>II', fdt, 32)
    strings = fdt[off_strings:off_strings+size_strings]
    p = off_struct; end = p+size_struct; stack=[]; phandles={}; power=[]
    while p < end:
        tok = struct.unpack_from('>I', fdt, p)[0]; p += 4
        if tok == 1:
            q = fdt.index(0, p); stack.append(fdt[p:q].decode()); p = (q+4)&~3
        elif tok == 2:
            stack.pop()
        elif tok == 3:
            length, nameoff = struct.unpack_from('>II', fdt, p); p+=8
            valpos=p; p=(p+length+3)&~3
            q=strings.index(0,nameoff); key=bytes(strings[nameoff:q]).decode()
            node='/'.join(stack)
            if key in ('phandle','linux,phandle') and length==4:
                phandles[struct.unpack_from('>I',fdt,valpos)[0]]=node
            if key=='gpios' and stack and stack[-1]=='gpio-poweroff':
                power.append((valpos,length,node))
        elif tok == 4:
            continue
        elif tok == 9:
            break
        else:
            raise ValueError('Unexpected FDT token')
    if len(power)!=1 or power[0][1]!=12:
        raise ValueError('Cannot uniquely locate gpio-poweroff gpios')
    pos, _, node = power[0]
    old=struct.unpack_from('>III',fdt,pos)
    if old[1:]!=(40,1) or 'gpio@10100' not in phandles.get(old[0],''):
        raise ValueError(f'Unexpected original GPIO definition: {old}')
    targets=[ph for ph,n in phandles.items() if n.endswith('gpio@10140')]
    if len(targets)!=1:
        raise ValueError(f'Cannot uniquely locate second GPIO controller: {targets}')
    # Tested power cut at physical HIGH: GPIO_ACTIVE_HIGH (flags=0).
    struct.pack_into('>III',fdt,pos,targets[0],8,0)
    result=bytearray(blob); result[fdtpos:fdtpos+size]=fdt
    struct.pack_into('>I',result,24,zlib.crc32(result[64:]))
    struct.pack_into('>I',result,4,0)
    struct.pack_into('>I',result,4,zlib.crc32(result[:64]))
    if zlib.crc32(result[64:])!=struct.unpack_from('>I',result,24)[0]:
        raise ValueError('CRC self-check failed')
    print(f'DTB: {node}: gpio0/40 active-low -> gpio1/8 active-high; uImage CRC verified')
    return bytes(result)


def assert_poweroff_gpio(kernel):
    """Check the final embedded FDT property, not only the patcher's log."""
    start = 64
    while True:
        p = kernel.find(MAGIC, start)
        if p < 0:
            raise ValueError('Patched kernel contains no valid FDT')
        if p + 40 <= len(kernel):
            size = struct.unpack_from('>I', kernel, p + 4)[0]
            if 40 <= size <= len(kernel) - p and struct.unpack_from('>I', kernel, p + 20)[0] == 17:
                break
        start = p + 4
    fdt = kernel[p:p+size]
    off_struct, off_strings = struct.unpack_from('>II', fdt, 8)
    size_strings, size_struct = struct.unpack_from('>II', fdt, 32)
    strings = fdt[off_strings:off_strings+size_strings]
    cursor = off_struct; end = cursor + size_struct; stack=[]; phandles={}; power=[]
    while cursor < end:
        token = struct.unpack_from('>I', fdt, cursor)[0]; cursor += 4
        if token == 1:
            q = fdt.index(0, cursor); stack.append(fdt[cursor:q].decode()); cursor=(q+4)&~3
        elif token == 2:
            stack.pop()
        elif token == 3:
            length, nameoff = struct.unpack_from('>II', fdt, cursor); cursor += 8
            valpos = cursor; cursor = (cursor+length+3)&~3
            q = strings.index(0, nameoff); key=bytes(strings[nameoff:q]).decode()
            if key in ('phandle','linux,phandle') and length == 4:
                phandles[struct.unpack_from('>I',fdt,valpos)[0]]='/'.join(stack)
            if key == 'gpios' and stack and stack[-1] == 'gpio-poweroff':
                power.append(struct.unpack_from('>III',fdt,valpos) if length==12 else None)
        elif token == 4:
            continue
        elif token == 9:
            break
        else:
            raise ValueError('Malformed patched FDT')
    if len(power) != 1 or power[0] is None:
        raise ValueError('Invalid gpio-poweroff property in patched FDT')
    ph, index, flags = power[0]
    if not phandles.get(ph, '').endswith('gpio@10140') or (index, flags) != (8,0):
        raise ValueError(f'Patched gpio-poweroff verification failed: {power[0]}')


def validate_mac(value):
    if not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}',value):
        raise ValueError('Invalid MAC syntax')
    raw=bytes.fromhex(value.replace(':',''))
    if raw[0]&1 or raw==bytes(6) or raw==b'\xff'*6:
        raise ValueError('MAC must be a nonzero unicast address')
    return value.upper()

MAC_SCRIPT='''#!/bin/sh
# /etc/uci-defaults: applied once at first boot; U-Boot environment untouched.
uci -q batch <<'EOT' || exit 1
set network.eth0_mac=device
set network.eth0_mac.name=eth0
set network.eth0_mac.macaddr=__MAC__
commit network
EOT
exit 0
'''


def put(root, rel, content, mode=0o644):
    path=root/rel; path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content,str) else content)
    path.chmod(mode)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('input',type=pathlib.Path)
    ap.add_argument('-o','--output',type=pathlib.Path)
    ap.add_argument('--mac',help='Optional MAC; otherwise prompt interactively')
    args=ap.parse_args()
    mac=validate_mac(args.mac or input('MAC (XX:XX:XX:XX:XX:XX): ').strip())
    for cmd in ('unsquashfs','mksquashfs','tar'):
        if not shutil.which(cmd): raise RuntimeError('Missing '+cmd+' (install squashfs-tools)')
    src=args.input.read_bytes()
    sha=hashlib.sha256(src).hexdigest()
    if sha!=EXPECTED: raise ValueError('Unexpected source SHA256: '+sha)
    metadata=extract_fwtool_metadata(src)
    out=args.output or args.input.with_name(args.input.stem+'-patched.bin')
    if out.resolve()==args.input.resolve(): raise ValueError('Refusing to overwrite input')
    with tarfile.open(fileobj=io.BytesIO(src),mode='r:') as archive:
        files={m.name:archive.extractfile(m).read() for m in archive if m.isfile()}
    if set(files)!={BASE+'CONTROL',BASE+'kernel',BASE+'root'} or files[BASE+'CONTROL']!=b'BOARD=netgear_stora\n':
        raise ValueError('Unexpected sysupgrade structure')
    kernel=patch_kernel(files[BASE+'kernel'])
    if len(kernel) > KERNEL_CAPACITY:
        raise ValueError(f'Kernel exceeds UBI volume: {len(kernel)} > {KERNEL_CAPACITY}')
    # Independently reparse patched uImage and assert exact FDT property bytes.
    assert_poweroff_gpio(kernel)
    sq=files[BASE+'root']
    if sq[:4]!=b'hsqs' or struct.unpack_from('<H',sq,20)[0]!=4 or struct.unpack_from('<H',sq,28)[0]!=4:
        raise ValueError('Expected SquashFS v4 with XZ compression')
    block=struct.unpack_from('<I',sq,12)[0]
    with tempfile.TemporaryDirectory(prefix='stora-patch-') as tmp:
        tmp=pathlib.Path(tmp); root=tmp/'rootfs'; original=tmp/'original.squashfs'; original.write_bytes(sq)
        run('unsquashfs','-no-progress','-d',root,original)
        put(root,'etc/uci-defaults/99-stora-mac',MAC_SCRIPT.replace('__MAC__',mac),0o755)
        newroot=tmp/'newroot.squashfs'
        # Try a few valid XZ layouts. Never silently emit an oversized image.
        # -no-exports removes an optional inode export table; single-threaded
        # builds can improve reproducibility. All candidates are verified below.
        candidates = [
            ('original-layout', ['-b', str(block)]),
            ('no-exports', ['-b', str(block), '-no-exports']),
            ('1MiB-blocks', ['-b', '1048576', '-no-exports']),
            ('512KiB-blocks', ['-b', '524288', '-no-exports']),
        ]
        best = None
        for label, extra in candidates:
            candidate=tmp/(label+'.squashfs')
            run('mksquashfs',root,candidate,'-noappend','-comp','xz',
                '-processors','1','-no-progress',*extra)
            size=candidate.stat().st_size
            print(f'SquashFS {label}: {size} bytes (limit {ROOTFS_CAPACITY})')
            if best is None or size < best[0]:
                best=(size,candidate)
            if size <= ROOTFS_CAPACITY:
                break
        if best[0] > ROOTFS_CAPACITY:
            raise ValueError(f'Rootfs exceeds existing UBI volume by '
                             f'{best[0]-ROOTFS_CAPACITY} bytes; no image produced. '
                             'Do not flash an oversized image.')
        shutil.copyfile(best[1],newroot)
        # Round-trip extraction verifies the embedded files and symlink.
        verify=tmp/'verify'; run('unsquashfs','-no-progress','-d',verify,newroot)
        for rel in ('etc/uci-defaults/99-stora-mac',):
            if (verify/rel).read_bytes()!=(root/rel).read_bytes():
                raise ValueError('SquashFS round-trip mismatch: '+rel)
        files[BASE+'kernel']=kernel; files[BASE+'root']=newroot.read_bytes()
        if len(files[BASE+'root']) > ROOTFS_CAPACITY:
            raise ValueError('Final rootfs too large')
        tempout=out.with_name(out.name+'.tmp')
        try:
            with tarfile.open(tempout,'w',format=tarfile.GNU_FORMAT) as archive:
                directory=tarfile.TarInfo(BASE)
                directory.type=tarfile.DIRTYPE
                directory.mode=0o755
                archive.addfile(directory)
                for name in (BASE+'CONTROL',BASE+'kernel',BASE+'root'):
                    data=files[name]; info=tarfile.TarInfo(name); info.size=len(data); info.mode=0o644
                    archive.addfile(info,io.BytesIO(data))
            with tarfile.open(tempout) as archive:
                members=archive.getmembers()
                if not members or members[0].name.rstrip('/') != BASE.rstrip('/') or not members[0].isdir():
                    raise ValueError('Missing first TAR directory entry required by nand_upgrade_tar')
                actual={m.name:archive.extractfile(m).read() for m in archive if m.isfile()}
                if actual != files:
                    raise ValueError('Archive content round-trip mismatch')
                # Mirror OpenWrt nand_upgrade_tar: tar tf - | grep -m 1 '^sysupgrade-.*/$'
                listing=subprocess.run(['tar','tf',str(tempout)],capture_output=True,text=True,check=True).stdout.splitlines()
                dirs=[line for line in listing if re.fullmatch(r'sysupgrade-.*/',line)]
                if dirs != [BASE] or listing[0] != BASE:
                    raise ValueError('TAR directory not discoverable by OpenWrt nand_upgrade_tar')
                assert_poweroff_gpio(actual[BASE+'kernel'])
                if len(actual[BASE+'root']) > ROOTFS_CAPACITY:
                    raise ValueError('Final TAR rootfs too large')
            append_fwtool_metadata(tempout, metadata)
            os.replace(tempout,out)
        finally:
            if tempout.exists(): tempout.unlink()
    print('fwtool metadata: restored and CRC verified')
    print('Output:',out)
    print('SHA256:',hashlib.sha256(out.read_bytes()).hexdigest())
    print('Changes: GPIO poweroff fix + first-boot MAC configuration only.')

if __name__=='__main__':
    try: main()
    except Exception as exc:
        print('ERROR:',exc,file=sys.stderr);sys.exit(1)
