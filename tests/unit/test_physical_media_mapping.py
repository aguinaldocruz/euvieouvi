"""Both wire formats must retain real paths, including nested Plex XML parts."""

from euvieouvi.connectors.jellyfin.mapper import map_item
from euvieouvi.connectors.plex.client import PlexPayload
from euvieouvi.connectors.plex.mapper import map_media_item, parse_container


def test_plex_xml_parts_and_versions_preserve_file_paths() -> None:
    payload = PlexPayload(
        content=b'<MediaContainer><Video ratingKey="1" type="movie" title="Film">'
        b'<Media><Part file="/media/film-part1.mkv"/><Part file="/media/film-part2.mkv"/></Media>'
        b'<Media><Part file="/media/film-part1.mkv"/></Media></Video></MediaContainer>',
        content_type="application/xml",
    )
    _, items = parse_container(payload)
    dto = map_media_item(items[0], "library")
    assert dto.file_paths == ("/media/film-part1.mkv", "/media/film-part2.mkv")


def test_jellyfin_path_and_media_sources_are_deduplicated() -> None:
    dto = map_item(
        {
            "Id": "1",
            "Type": "Movie",
            "Name": "Film",
            "Path": "/media/film.mkv",
            "MediaSources": [{"Path": "/media/film.mkv"}, {"Path": "/media/film-4k.mkv"}],
        },
        "library",
    )
    assert dto.file_paths == ("/media/film-4k.mkv", "/media/film.mkv")
