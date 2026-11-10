"""Parse the NI::SOUND::Item tree of an FM8 .nfm8 chunk.

Grammar recovered from the macOS 1.4.6 writer functions:

  ItemHeaderWriter : u64 size, u32 1, 'hsin', u32 1, u32 0, uuid[16], body
  Item::writeItem  : u64 size, 'DSIN', u32 itemID, u32 a, u32 b, u32 c, ...
  SubtreeItem      : u64 size, 'DSIN', u32 0x73, u32 1, <Item>, ...

`size` always counts itself, i.e. a node spans [pos, pos+size).
"""
import struct


class Node:
    __slots__ = ('pos', 'size', 'kind', 'item', 'uuid', 'children')

    def __init__(self, pos, size, kind, item=None, uuid=None):
        self.pos, self.size, self.kind = pos, size, kind
        self.item, self.uuid, self.children = item, uuid, []

    def __repr__(self):
        return '<%s %s item=%s size=%d pos=%#x>' % (
            self.kind, '', self.item, self.size, self.pos)


def parse_tree(data):
    """Walk the item tree. Returns (root, consumed, leftover_notes)."""
    notes = []

    def node(pos, limit):
        if pos + 8 > limit:
            return None
        size = struct.unpack_from('<Q', data, pos)[0]
        if size < 16 or pos + size > limit:
            return None
        tag = data[pos + 8:pos + 12]
        if tag == b'DSIN':
            item = struct.unpack_from('<I', data, pos + 12)[0]
            n = Node(pos, size, 'DSIN', item)
            child = pos + 16
            # SubtreeItem wraps an Item: u64 size,'DSIN'... starting at pos+20
            while child + 16 <= pos + size:
                sz2 = struct.unpack_from('<Q', data, child)[0]
                if sz2 < 16 or child + sz2 > pos + size or data[child + 8:child + 12] != b'DSIN':
                    break
                sub = node(child, pos + size)
                if sub is None:
                    break
                n.children.append(sub)
                child += sz2
            return n
        if data[pos + 8:pos + 12] == b'\x01\x00\x00\x00' and data[pos + 12:pos + 16] == b'hsin':
            size_ok = struct.unpack_from('<Q', data, pos)[0]
            n = Node(pos, size_ok, 'hsin', None, data[pos + 24:pos + 40])
            body = pos + 40
            while body + 16 <= pos + n.size:
                sz2 = struct.unpack_from('<Q', data, body)[0]
                if sz2 < 16 or body + sz2 > pos + n.size:
                    break
                sub = node(body, pos + n.size)
                if sub is None:
                    break
                n.children.append(sub)
                body += sz2
            return n
        return None

    root = node(0, len(data))
    consumed = root.size if root else 0
    return root, consumed, notes


def dump(node, data, depth=0, out=None):
    out = out if out is not None else []
    nxt = node.pos + node.size
    tail = data[nxt:nxt + 12] if nxt < len(data) else b''
    out.append('%s%s item=%-4s size=%-5d [%#06x..%#06x)  next=%s' % (
        '  ' * depth, node.kind, node.item, node.size, node.pos, nxt, tail.hex(' ')))
    for c in node.children:
        dump(c, data, depth + 1, out)
    return out


if __name__ == '__main__':
    import sys
    for path in sys.argv[1:]:
        data = open(path, 'rb').read()
        root, consumed, _ = parse_tree(data)
        print('==', path, len(data), 'consumed', consumed)
        if root:
            for line in dump(root, data):
                print('  ', line)
        else:
            print('   parse failed')
