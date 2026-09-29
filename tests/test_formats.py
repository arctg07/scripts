import random
import unittest

from helpers import SCRIPTS_DIR  # noqa: F401 — добавляет scripts/ в sys.path

from transfer import TransferError, formats


def parts_for(payload, kind="changes", project="my project/ё", max_lines=5):
    sha = formats.sha256(payload) if kind == "changes" else formats.sha256(formats.decode_base64(payload))
    return formats.make_parts(kind, project, "2026-01-01_00-00-00-abcd", payload, sha, max_lines)


def scan_all(blobs):
    items = []
    for i, data in enumerate(blobs):
        items.extend(formats.scan_file(data, "f%d" % i))
    return items


class HeaderTest(unittest.TestCase):
    def test_roundtrip_with_spaces_and_unicode(self):
        line = formats.make_header("snapshot", "проект с пробелом", "id-1", 2, 3, 10, "ab" * 32)
        hdr = formats.parse_header(line)
        self.assertEqual(hdr["project"], "проект с пробелом")
        self.assertEqual((hdr["index"], hdr["total"], hdr["lines"]), (2, 3, 10))

    def test_not_a_header(self):
        self.assertIsNone(formats.parse_header(b"# PROJECT: x"))

    def test_broken_header(self):
        with self.assertRaises(TransferError):
            formats.parse_header(b"#@transfer v1 kind=changes project=x")


class PartsTest(unittest.TestCase):
    payload = b"".join(b"line %d\n" % i for i in range(23))

    def test_parts_respect_limit_and_assemble_in_any_order(self):
        parts = parts_for(self.payload)
        self.assertEqual(len(parts), 6)  # 23 строки по 4 в части (+ заголовок = 5)
        for p in parts:
            self.assertLessEqual(p.count(b"\n"), 5)
        random.Random(1).shuffle(parts)
        items = scan_all(parts)
        kind, data = formats.assemble_parts([i for i in items if i["type"] == "part"])
        self.assertEqual((kind, data), ("changes", self.payload))

    def test_several_parts_in_one_file(self):
        parts = parts_for(self.payload)
        items = scan_all([parts[2] + parts[0], b"\n" + parts[1] + b"\n\n", b"".join(parts[3:])])
        self.assertEqual(formats.assemble_parts([i for i in items if i["type"] == "part"])[1], self.payload)

    def test_missing_part(self):
        parts = parts_for(self.payload)
        items = scan_all(parts[:2] + parts[3:])
        with self.assertRaisesRegex(TransferError, "не хватает частей: 3"):
            formats.assemble_parts([i for i in items if i["type"] == "part"])

    def test_corrupted_part(self):
        parts = parts_for(self.payload)
        parts[1] = parts[1].replace(b"line 5", b"line 6")
        items = scan_all(parts)
        with self.assertRaisesRegex(TransferError, "контрольная сумма"):
            formats.assemble_parts([i for i in items if i["type"] == "part"])

    def test_truncated_part(self):
        parts = parts_for(self.payload)
        parts[1] = b"\n".join(parts[1].split(b"\n")[:3])
        items = scan_all(parts)
        errors = [i for i in items if i["type"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("обрезана", errors[0]["message"])

    def test_truncated_part_followed_by_next_part(self):
        parts = parts_for(self.payload)
        items = scan_all([b"\n".join(parts[1].split(b"\n")[:3]) + b"\n" + parts[2]])
        self.assertIn("обрезана", [i for i in items if i["type"] == "error"][0]["message"])
        self.assertEqual([i["header"]["index"] for i in items if i["type"] == "part"], [3])

    def test_header_like_line_in_content(self):
        # Пример заголовка в README переносимого файла — это содержимое, а не начало новой части.
        payload = (b"FILE: README.md\n```\n#@transfer v1 kind=changes project=x id=2026-09-25_14-00-00-a1b2 "
                   b"part=1/3 lines=6999 sha256=\xe2\x80\xa6\n#@transfer v1 kind=changes project=x id=y part=1/1 "
                   b"lines=500 sha256=" + b"ab" * 32 + b"\n```\n") + self.payload
        parts = parts_for(payload, max_lines=100)
        items = scan_all(parts)
        self.assertEqual([i["type"] for i in items], ["part"])
        self.assertEqual(formats.assemble_parts(items)[1], payload)

    def test_editor_stripped_final_newline(self):
        parts = [p.rstrip(b"\n") for p in parts_for(self.payload)]
        items = scan_all(parts)
        self.assertEqual(formats.assemble_parts([i for i in items if i["type"] == "part"])[1], self.payload)

    def test_crlf_transport(self):
        parts = [p.replace(b"\n", b"\r\n") for p in parts_for(self.payload)]
        items = scan_all(parts)
        self.assertEqual(formats.assemble_parts([i for i in items if i["type"] == "part"])[1], self.payload)

    def test_snapshot_kind_decodes_base64(self):
        raw = bytes(range(256)) * 20
        parts = parts_for(formats.encode_base64(raw), kind="snapshot", max_lines=8)
        items = scan_all(parts)
        self.assertEqual(formats.assemble_parts([i for i in items if i["type"] == "part"]), ("snapshot", raw))


class ChangesFormatTest(unittest.TestCase):
    def test_roundtrip_exact_endings(self):
        files = [("a.txt", b"x\n"), ("no-eol.txt", b"x"), ("blank-tail.txt", b"x\n\n\n"), ("empty.txt", b""),
                 ("crlf.txt", b"a\r\nb\r\n"), ("only-newlines.txt", b"\n\n")]
        meta = []
        for path, content in files:
            eol = formats.eol_count(content)
            if eol != formats.default_eol(content.rstrip(b"\n")):
                meta.append("EOL: %d %s" % (eol, path))
        payload = formats.build_changes(meta, files, ["gone.txt"])
        parsed = formats.parse_changes(payload)
        self.assertEqual(parsed["files"], files)
        self.assertEqual(parsed["deleted"], ["gone.txt"])

    def test_legacy_without_metadata(self):
        legacy = (b"=" * 64 + b"\nFILE: src/A.java\n" + b"=" * 64 + b"\nclass A {}\n\n\n"
                  + b"=" * 64 + b"\nDELETED FILES\n" + b"=" * 64 + b"\nsrc/B.java\n")
        parsed = formats.parse_changes(legacy)
        self.assertEqual(parsed["files"], [("src/A.java", b"class A {}\n")])
        self.assertEqual(parsed["deleted"], ["src/B.java"])
        self.assertEqual(scan_all([legacy])[0]["type"], "legacy-changes")


class SafePathTest(unittest.TestCase):
    def test_rejects_dangerous_paths(self):
        for bad in ("../x", "/etc/passwd", "a/../../b", ".git/hooks/pre-commit", "a//b", "C:/x", ""):
            with self.assertRaises(TransferError, msg=bad):
                formats.safe_rel_path(bad)
        self.assertEqual(formats.safe_rel_path("src/main/файл й.txt"), "src/main/файл й.txt")


if __name__ == "__main__":
    unittest.main()
