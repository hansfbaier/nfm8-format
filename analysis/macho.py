"""Minimal Mach-O (incl. fat) parser for symbol / section inspection."""
import struct

FAT_MAGIC = 0xcafebabe
MH_MAGICS = {0xfeedface: 32, 0xfeedfacf: 64}
LC_SYMTAB, LC_SEGMENT, LC_SEGMENT_64, LC_DYSYMTAB = 0x2, 0x1, 0x19, 0xb
CPU = {7: 'x86', 0x01000007: 'x86_64', 12: 'arm', 0x0100000c: 'arm64'}


class Slice:
    def __init__(self, data, off, size, cputype, cpusub):
        self.data = data
        self.off = off
        self.size = size
        self.cputype = cputype
        self.cpusub = cpusub
        self._parse()

    def _parse(self):
        d, o = self.data, self.off
        magic = struct.unpack_from('<I', d, o)[0]
        self.is64 = magic == 0xfeedfacf
        self.cputype = struct.unpack_from('<I', d, o+4)[0]
        ncmds = struct.unpack_from('<I', d, o+16)[0]
        sizeofcmds = struct.unpack_from('<I', d, o+20)[0]
        self.flags = struct.unpack_from('<I', d, o+24)[0]
        p = o + (32 if self.is64 else 28)
        self.segments, self.sections, self.symtab = [], [], None
        for _ in range(ncmds):
            cmd, cmdsize = struct.unpack_from('<II', d, p)
            if cmd in (LC_SEGMENT_64, LC_SEGMENT):
                s64 = cmd == LC_SEGMENT_64
                nm = d[p+8:p+24].rstrip(b'\0').decode('latin1')
                if s64:
                    vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', d, p+24)
                    nsects = struct.unpack_from('<I', d, p+64)[0]
                    sp = p + 72
                else:
                    vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<IIII', d, p+24)
                    nsects = struct.unpack_from('<I', d, p+48)[0]
                    sp = p + 56
                self.segments.append((nm, vmaddr, vmsize, fileoff, filesize))
                for k in range(nsects):
                    if s64:
                        sn = d[sp:sp+16].rstrip(b'\0').decode('latin1')
                        seg = d[sp+16:sp+32].rstrip(b'\0').decode('latin1')
                        addr, size = struct.unpack_from('<QQ', d, sp+32)
                        off_, align, reloff, nreloc, flags_ = struct.unpack_from('<IIIII', d, sp+48)
                        sp += 80
                    else:
                        sn = d[sp:sp+16].rstrip(b'\0').decode('latin1')
                        seg = d[sp+16:sp+32].rstrip(b'\0').decode('latin1')
                        addr, size = struct.unpack_from('<II', d, sp+32)
                        off_, align, reloff, nreloc, flags_ = struct.unpack_from('<IIIII', d, sp+40)
                        sp += 68
                    self.sections.append((seg, sn, addr, size, off_))
            elif cmd == LC_SYMTAB:
                symoff, nsyms, stroff, strsize = struct.unpack_from('<IIII', d, p+8)
                self.symtab = (symoff, nsyms, stroff, strsize)
            p += cmdsize

    def read(self, addr, n):
        for nm, vmaddr, vmsize, fileoff, filesize in self.segments:
            if vmaddr <= addr < vmaddr + max(vmsize, filesize):
                d = addr - vmaddr
                if d < filesize:
                    return self.data[self.off + fileoff + d: self.off + fileoff + d + n]
        return None

    def disasm(self, addr, count=40):
        import capstone
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        code = self.read(addr, 15*count)
        out = []
        for ins in md.disasm(code, addr):
            out.append(ins)
            if len(out) >= count: break
        return out

    def symbols(self, limit=None):
        if not self.symtab: return []
        symoff, nsyms, stroff, strsize = self.symtab
        d, base = self.data, self.off
        symoff += base
        stroff += base
        out = []
        for i in range(nsyms):
            if self.is64:
                e = symoff + i*16
                strx, ntype, nsect, ndesc, nvalue = struct.unpack_from('<IBBHQ', d, e)
            else:
                e = symoff + i*12
                strx, ntype, nsect, ndesc, nvalue = struct.unpack_from('<IBBhI', d, e)
            if strx >= strsize: continue
            s = stroff + strx
            end = d.find(b'\0', s)
            name = d[s:end].decode('latin1', 'replace')
            out.append((name, ntype, nvalue))
            if limit and len(out) >= limit: break
        return out


def slices(path):
    d = open(path, 'rb').read()
    magic = struct.unpack_from('>I', d, 0)[0]
    if magic == FAT_MAGIC:
        n = struct.unpack_from('>I', d, 4)[0]
        out = []
        for i in range(n):
            cpu, sub, off, size, align = struct.unpack_from('>IIIII', d, 8 + i*20)
            out.append(Slice(d, off, size, cpu, sub))
        return out
    return [Slice(d, 0, len(d), None, None)]


if __name__ == '__main__':
    import sys
    for s in slices(sys.argv[1]):
        syms = s.symbols()
        print('slice %s/%#x size %#x  segs %d secs %d  symbols %d  flags %#x' % (
            CPU.get(s.cputype, hex(s.cputype)), s.cpusub, s.size,
            len(s.segments), len(s.sections), len(syms), s.flags))
        for name, ntype, nvalue in syms[:8]:
            print('   %#010x type %#04x %s' % (nvalue, ntype, name[:100]))
