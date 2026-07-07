#!/usr/bin/env python3
"""Вставляет <item> релиза в appcast.xml (Sparkle auto-update).

Только stdlib. Вставка строковая (перед </channel>) — ElementTree ломает
namespace-префиксы при round-trip; валидность выхода проверяется парсом.

Usage:
    python3 scripts/generate_appcast_item.py \
        --appcast appcast.xml --version 2.4.0 \
        --url https://github.com/Pavua/Krab-Ear/releases/download/v2.4.0/Krab-Ear-v2.4.0.zip \
        --ed-signature "BASE64SIG==" --length 7080544
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+\Z")
_VERSION_ATTR_RE = re.compile(r'sparkle:version="(\d+\.\d+\.\d+)"')


def _parse_semver(version: str) -> tuple[int, int, int]:
    if not _SEMVER_RE.match(version):
        raise ValueError(f"версия не semver X.Y.Z: {version!r}")
    a, b, c = version.split(".")
    return (int(a), int(b), int(c))


def add_item(xml_text: str, *, version: str, url: str,
             ed_signature: str, length: int, pub_date: str | None = None) -> str:
    """Возвращает appcast с добавленным <item>. ValueError при немонотонной версии."""
    new_v = _parse_semver(version)
    existing = [_parse_semver(v) for v in _VERSION_ATTR_RE.findall(xml_text)]
    if existing and new_v <= max(existing):
        raise ValueError(
            f"версия {version} не больше максимальной в appcast "
            f"({'.'.join(map(str, max(existing)))}) — Sparkle требует монотонность")
    if "  </channel>" not in xml_text:
        raise ValueError("appcast без </channel> — не скелет Sparkle-фида")

    if pub_date is None:
        pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    item = f"""  <item>
    <title>Krab Ear {version}</title>
    <pubDate>{pub_date}</pubDate>
    <sparkle:minimumSystemVersion>13.0</sparkle:minimumSystemVersion>
    <enclosure url="{url}"
               sparkle:version="{version}"
               sparkle:shortVersionString="{version}"
               length="{length}"
               sparkle:edSignature="{ed_signature}"
               type="application/octet-stream"/>
  </item>
"""
    out = xml_text.replace("  </channel>", item + "  </channel>", 1)
    ET.fromstring(out)  # self-check: выход обязан быть валидным XML
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--appcast", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--ed-signature", required=True)
    p.add_argument("--length", required=True, type=int)
    args = p.parse_args()
    with open(args.appcast, encoding="utf-8") as f:
        xml_text = f.read()
    out = add_item(xml_text, version=args.version, url=args.url,
                   ed_signature=args.ed_signature, length=args.length)
    with open(args.appcast, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"appcast: добавлен item v{args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
