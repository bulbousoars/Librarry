from librarry.indexers import _parse_newznab_xml

TORZNAB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"
     xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <item>
      <title>Some Book EPUB</title>
      <link>http://dl/torrent</link>
      <guid>abc123</guid>
      <pubDate>Mon, 02 Jan 2023 15:04:05 +0000</pubDate>
      <enclosure url="http://dl/torrent" length="123456" type="application/x-bittorrent"/>
      <newznab:attr name="size" value="123456"/>
      <newznab:attr name="seeders" value="42"/>
      <newznab:attr name="peers" value="50"/>
      <newznab:attr name="grabs" value="7"/>
      <newznab:attr name="category" value="7020"/>
    </item>
  </channel>
</rss>"""

USENET_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <item>
      <title>Another Book MOBI</title>
      <link>http://dl/nzb</link>
      <guid>z9</guid>
      <newznab:attr name="size" value="999"/>
      <newznab:attr name="grabs" value="13"/>
    </item>
  </channel>
</rss>"""


def test_parses_torrent_seeders_leechers_age():
    out = _parse_newznab_xml(TORZNAB_XML, "Jackett", "torrent")
    assert len(out) == 1
    c = out[0]
    assert c.size_bytes == 123456
    assert c.seeders == 42
    assert c.leechers == 8          # peers(50) - seeders(42)
    assert c.grabs == 7
    assert c.pub_date.startswith("Mon, 02 Jan 2023")
    assert c.category == "7020"


def test_parses_usenet_grabs_without_seeders():
    out = _parse_newznab_xml(USENET_XML, "NZBGeek", "usenet")
    c = out[0]
    assert c.grabs == 13
    assert c.seeders is None and c.leechers is None
    assert c.size_bytes == 999
