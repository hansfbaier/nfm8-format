"""Minimal PE helper for FM8 static analysis (pefile + capstone)."""
import pefile, capstone

DLL = r'C:\Program Files\Native Instruments\VSTPlugins 64 bit\FM8.dll'

class Bin:
    def __init__(self, path=DLL):
        self.path = path
        self.pe = pefile.PE(path, fast_load=True)
        self.data = open(path, 'rb').read()
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)

    def sec_of(self, off):
        for s in self.pe.sections:
            start = s.PointerToRawData
            if start <= off < start + s.SizeOfRawData:
                return s
        return None

    def rva(self, off):
        s = self.sec_of(off)
        return None if s is None else s.VirtualAddress + (off - s.PointerToRawData)

    def off(self, rva):
        for s in self.pe.sections:
            if s.VirtualAddress <= rva < s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData):
                d = rva - s.VirtualAddress
                if d < s.SizeOfRawData:
                    return s.PointerToRawData + d
        return None

    def disasm(self, rva, count=40):
        o = self.off(rva)
        if o is None: return []
        out = []
        for ins in self.md.disasm(self.data[o:o + 15*count], self.base + rva):
            out.append(ins)
            if len(out) >= count: break
        return out

    def find(self, pat):
        out, i = [], self.data.find(pat)
        while i >= 0:
            out.append(i); i = self.data.find(pat, i+1)
        return out

if __name__ == '__main__':
    b = Bin()
    print('image base %#x, sections %d' % (b.base, len(b.pe.sections)))
    for off in b.find(b'DSIN')[:6]:
        print('DSIN at file %#x -> rva %#x' % (off, b.rva(off)))
