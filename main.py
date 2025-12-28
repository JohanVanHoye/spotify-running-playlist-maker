import time as tm
from datetime import datetime
from models import Artist
from settings import Settings
from playlists import load_playlists, count_tracks_in_playlist, get_next_target_playlist, add_playlist_tracks


# load and validate user settings from toml file
# this object also contains SQL database engine and Spotipy client.
settings = Settings()


def discover_artists_for_genre(
    _settings,
    primary_market='BE',
    max_track_pages=20,          # 20 * 50 = ~1000 tracks scanned
    playlist_pages=0,            # scan first N playlist search pages
    expand_related=False,        # optionally expand via related artists
):
    """
    Genre-agnostic artist discovery.
    Strategy:
      A) Try artist search for the genre in the primary market; if empty, try a few fallback markets.
      B) If still sparse, search tracks with the same query in the primary market and derive artists from track credits.
      C) Harvest playlists whose title/description contains the genre term and derive artists from their tracks.
      D) Optionally expand the discovered set via one-hop 'related artists'.
    Returns: set[Artist]
    """
    spotify_client = _settings.spotify

    # Build query safely from user-provided filters
    genre_query = (_settings.genre_searchstring or '').strip()
    artist_hint = (_settings.artist_searchstring or '').strip()
    query_parts = []
    if genre_query:
        query_parts.append(f'genre:"{genre_query}"')
    if artist_hint:
        query_parts.append(f'artist:"{artist_hint}"')
    query = ' '.join(query_parts)

    if not query:
        print("⚠ No genre/artist filters; discovery skipped.")
        return set()

    # Dedup store keyed by artist ID
    discovered_artists_by_id = {}  # artist_id -> (artist_name, artist_uri)

    # ---- A) Artist search in primary, then fallback markets ----
    candidate_markets = [primary_market, 'US', 'GB', 'DE', 'FR', 'NL']
    for market_code in candidate_markets:
        try:
            search_response = spotify_client.search(q=query, market=market_code, type='artist', limit=50, offset=0)
            artist_items = search_response.get('artists', {}).get('items', []) or []
            for artist in artist_items:
                artist_id = artist['uri'].split(':')[-1]
                discovered_artists_by_id[artist_id] = (artist['name'], artist['uri'])
            if artist_items:
                print(f"✓ Found {len(artist_items)} artist(s) for {query!r} in market {market_code}")
                break  # Stop once we have any artist hits
            else:
                print(f"… No artists for {query!r} in market {market_code}, trying next market")
        except Exception as ex:
            print(f"⚠ Artist search failed in {market_code}: {ex}")

    # ---- B) Track search fallback (genre -> derive artists) ----
    if not discovered_artists_by_id:
        try:
            for page_index in range(max_track_pages):  # paginate tracks, derive many artists
                offset = page_index * 50
                track_search_response = spotify_client.search(q=query, market=primary_market,
                                                              type='track', limit=50, offset=offset)
                track_items = track_search_response.get('tracks', {}).get('items', []) or []
                if not track_items:
                    break
                for track in track_items:
                    for track_artist in (track.get('artists') or []):
                        # Prefer 'id'; fallback to 'uri' only if present and well-formed.
                        artist_id = track_artist.get('id')
                        artist_uri = track_artist.get('uri')
                        if artist_id:
                            artist_uri = artist_uri or f"spotify:artist:{artist_id}"
                        elif artist_uri:
                            uri_parts = artist_uri.split(':')
                            if len(uri_parts) == 3 and uri_parts[1] == 'artist':
                                artist_id = uri_parts[2]
                            else:
                                continue  # malformed or non-artist URI; skip
                        else:
                            continue  # neither id nor uri; skip
                        discovered_artists_by_id[artist_id] = (track_artist.get('name') or '(unknown)', artist_uri)
            if discovered_artists_by_id:
                print(
                    f"✓ Derived {len(discovered_artists_by_id)} artists from tracks for {query!r} in {primary_market}")
            else:
                print(f"… No artists derived from tracks for {query!r} in {primary_market}")
        except Exception as ex:
            print(f"⚠ Track search failed in {primary_market}: {ex}")

    # ---- C) Playlist harvest (broad coverage), optional ----
    try:
        playlist_search_term = genre_query  # free text search; not a field filter here
        for page_index in range(playlist_pages):
            offset = page_index * 50
            playlist_search_response = spotify_client.search(q=playlist_search_term, type='playlist',
                                                             market=primary_market, limit=50, offset=offset)
            playlist_items = playlist_search_response.get('playlists', {}).get('items', []) or []
            if not playlist_items:
                break
            for playlist in playlist_items:
                if not playlist:
                    continue
                playlist_id = playlist.get('id')
                if not playlist_id:
                    continue
                playlist_offset = 0
                # Pull up to ~200 items per playlist (tune as needed)
                while playlist_offset <= 200:
                    playlist_page = spotify_client.playlist_items(playlist_id, market=primary_market,
                                                                  limit=100, offset=playlist_offset)
                    track_rows = playlist_page.get('items', []) or []
                    if not track_rows:
                        break
                    for row in track_rows:
                        track_obj = row.get('track')
                        if not track_obj or track_obj.get('type') != 'track':
                            continue
                        for track_artist in track_obj.get('artists', []) or []:
                            artist_uri = track_artist.get('uri')
                            if not artist_uri:
                                continue
                            artist_id = artist_uri.split(':')[-1]
                            discovered_artists_by_id[artist_id] = (track_artist['name'], artist_uri)
                    if not playlist_page.get('next'):
                        break
                    playlist_offset += 100
                if len(discovered_artists_by_id) % 100 == 0:
                    print(f" ... Playlist harvesting ongoing; total artists now {len(discovered_artists_by_id)}")
        if discovered_artists_by_id:
            print(f"✓ Playlist harvest added; total artists now {len(discovered_artists_by_id)}")
    except Exception as ex:
        print(f"⚠ Playlist harvest failed: {ex}")

    # ---- D) Related artists expansion (1 hop) ----
    if expand_related and discovered_artists_by_id:
        seed_artist_ids = list(discovered_artists_by_id.keys())
        # Cap expansion to avoid rate limits; 500 seeds is usually plenty
        for seed_id in seed_artist_ids[:500]:
            try:
                related_response = spotify_client.artist_related_artists(seed_id)
                for related in related_response.get('artists', []) or []:
                    related_id = related['uri'].split(':')[-1]
                    discovered_artists_by_id[related_id] = (related['name'], related['uri'])
            except Exception as ex:
                print(f"⚠ Related artists fetch failed for {seed_id}: {ex}")
        print(f"✓ Related expansion done; total artists now {len(discovered_artists_by_id)}")

    return {Artist(name=val[0], uri=val[1]) for val in discovered_artists_by_id.values()}


# def discover_artists_for_genre(
#     stngs,
#     primary_market='BE',
#     max_track_pages=20,     # 20 * 50 = ~1000 tracks scanned
#     playlist_pages=8,       # scan first N playlist search pages
#     expand_related=True,    # optionally expand via related artists
# ):
#     """
#     Genre-agnostic artist discovery.
#
#     Strategy:
#       A) Try artist search for the genre in BE; if empty, try a few fallback markets.
#       B) If still sparse, search tracks by the same genre in BE, extract artists.
#       C) Optionally expand discovered set via 'related artists' (1 hop).
#
#     Returns: set[Artist]
#     """
#     sp = stngs.spotify
#     genre = stngs.genre_searchstring
#
#     # Inputs & safe query parts
#     genre = (genre or stngs.genre_searchstring or "").strip()
#     artist_hint = (stngs.artist_searchstring or "").strip()
#
#     parts = []
#     if genre:
#         parts.append(f'genre:"{genre}"')
#     if artist_hint:
#         parts.append(f'artist:"{artist_hint}"')
#     q = " ".join(parts)
#     if not q:
#         # If the user didn't provide any filters, bail early.
#         print("⚠ No genre/artist filters; discovery skipped.")
#         return set()
#
#     # Dedup store keyed by artist ID
#     found = {}  # artist_id -> (name, uri)
#
#     # ---------- A) Artist search in BE, then fallback markets ----------
#     markets = [primary_market, 'US', 'GB', 'DE', 'FR', 'NL']
#     for mk in markets:
#         try:
#             res = sp.search(q=q, market=mk, type='artist', limit=50, offset=0)
#             items = res.get('artists', {}).get('items', []) or []
#             for a in items:
#                 aid = a['uri'].split(':')[-1]
#                 found[aid] = (a['name'], a['uri'])
#             if items:
#                 print(f"✓ Found {len(items)} artist(s) for {q!r} in market {mk}")
#                 break  # Stop once we have any artist hits
#             else:
#                 print(f"… No artists for {q!r} in market {mk}, trying next market")
#         except Exception as ex:
#             print(f"⚠ Artist search failed in {mk}: {ex}")
#
#     # ---------- B) Track search fallback (genre -> derive artists) ----------
#     if not found:
#         try:
#             for page in range(max_track_pages):  # paginate tracks, derive many artists
#                 offset = page * 50
#                 tres = sp.search(q=q, market=primary_market, type='track', limit=50, offset=offset)
#                 titems = tres.get('tracks', {}).get('items', []) or []
#                 if not titems:
#                     break
#                 for t in titems:
#                     for ar in (t.get('artists') or []):
#                       #  Prefer 'id'; fallback to 'uri' only if present and well-formed.
#                         aid = ar.get('id')
#                         auri = ar.get('uri')
#                         if aid:
#                             auri = auri or f"spotify:artist:{aid}"
#                         elif auri:
#                             parts = auri.split(':')
#                             if len(parts) == 3 and parts[1] == 'artist':
#                                 aid = parts[2]
#                             else:
#                                 continue  # malformed or non-artist URI; skip
#                         else:
#                             continue  # neither id nor uri; skip
#                         found[aid] = (ar.get('name') or '(unknown)', auri)
#             if found:
#                 print(f"✓ Derived {len(found)} artist(s) from tracks for {q!r} in {primary_market}")
#             else:
#                 print(f"… No artists derived from tracks for {q!r} in {primary_market}")
#         except Exception as ex:
#             print(f"⚠ Track search failed in {primary_market}: {ex}")
#
#     # ---------- C) Playlist harvest (broad coverage), optional ----------
#     # Search playlists by the genre term itself; pull tracks -> extract artists.
#     # Useful when artist genres are sparse but curators use the term in titles/descriptions.
#     try:
#         term = genre  # search text; not a field filter here
#         for page in range(playlist_pages):
#             offset = page * 50
#             pres = sp.search(q=term, type='playlist', market=primary_market, limit=50, offset=offset)
#             pls = pres.get('playlists', {}).get('items', []) or []
#             if not pls:
#                 break
#             for pl in pls:
#                 if pl:
#                     pl_id = pl.get('id')
#                 if not pl_id:
#                     continue
#                 pl_offset = 0
#                 # Pull up to ~200 items per playlist (tune as needed)
#                 while pl_offset <= 200:
#                     ppage = sp.playlist_items(pl_id, market=primary_market, limit=100, offset=pl_offset)
#                     trs = ppage.get('items', []) or []
#                     if not trs:
#                         break
#                     for it in trs:
#                         tr = it.get('track')
#                         if not tr or tr.get('type') != 'track':
#                             continue
#                         for ar in tr.get('artists', []) or []:
#                             if not ar.get('uri'):
#                                 continue
#                             aid = ar['uri'].split(':')[-1]
#                             found[aid] = (ar['name'], ar['uri'])
#                     if not ppage.get('next'):
#                         break
#                     pl_offset += 100
#                     if len(found) % 100 == 0:
#                         print(f"    ... Playlist harvesting ongoing; total artists now {len(found)}")
#         if found:
#             print(f"✓ Playlist harvest added; total artists now {len(found)}")
#     except Exception as ex:
#         print(f"⚠ Playlist harvest failed: {ex}")
#
#     # ---------- D) Related artists expansion (1 hop) ----------
#     if expand_related and found:
#         seeds = list(found.keys())
#         # Cap expansion to avoid rate limits; 500 seeds is usually plenty
#         for seed_id in seeds[:500]:
#             try:
#                 rel = sp.artist_related_artists(seed_id)
#                 for a in rel.get('artists', []) or []:
#                     aid = a['uri'].split(':')[-1]
#                     found[aid] = (a['name'], a['uri'])
#             except Exception as ex:
#                 print(f"⚠ Related artists fetch failed for {seed_id}: {ex}")
#         print(f"✓ Related expansion done; total artists now {len(found)}")
#
#     return {Artist(name=v[0], uri=v[1]) for v in found.values()}


def main():
    """
    Creates playlists based on search criteria.
    i) Searches Spotify for artists in a given genre
    ii) Loops over artists and retrieves all albums of each artist
    iii) gets tracks of each album and fetches their tempo
    iv) keeps only tracks within desired tempo range
    v) saves these tracks to one or more temporary new playlist(s),
       if they did not already appear in one or more other playlists you might have.
    """
    print('Spotify Run List maker started at ', datetime.now())

    cur = settings.sql_cursor

    print(f"Welcome", settings.spotify.me()["display_name"], "☺")

    # Find all artists using Search for Genre
    # artists_to_process = set()
    # my_limit = 50
    # my_offset = 0
    # api_result = None
    match_found = False
    # q = ''
    # if settings.genre_searchstring and settings.genre_searchstring.strip():
    #     q = f'genre:"{settings.genre_searchstring}" '
    # if settings.artist_searchstring and settings.artist_searchstring.strip():
    #     q += f'artist:"{settings.artist_searchstring}"'
    #
    # while my_offset == 0 or (api_result['artists']['next'] and my_offset < 1000):
    #     api_result = settings.spotify.search(q=q, market='BE', type='artist', limit=my_limit, offset=my_offset)
    #     for searchResult in api_result['artists']['items']:
    #         artists_to_process.add(Artist(searchResult["name"], searchResult["uri"]))
    #     my_offset += my_limit

    artists_to_process = discover_artists_for_genre(settings, primary_market='BE')
    print('Search for artists in genre', settings.genre_searchstring, 'yielded', len(artists_to_process), 'results.')

    process = "?"
    for artist in artists_to_process:
        print('')
        print('♫', artist.name.upper())
        print('=' * 80)
        if settings.interactive_mode:
            if process not in ("A", "a"):
                process = "?"
            while process not in ("", "Y", "N", "C", "A", "y", "n", "c", "a"):
                process = input("Process this artist? (Enter = Yes; N = No, skip; C = Cancel, "
                                "stop processing any more artists, A = Yes to all): ") or "Y"
        else:
            # all artists are processed in non-interactive mode
            process = "A"
        if process in ("C", "c"):
            break
        if process in ("A", "a", "Y", "y"):
            results = settings.spotify.artist_albums(artist_id=artist.uri, include_groups='album,single')
            albums = results['items']
            while results['next']:
                results = settings.spotify.next(results)
                albums.extend(results['items'])

            for album in albums:
                print('◌', album['name'])
                while True:
                    try:
                        album = settings.spotify.album(album['uri'])
                    except Exception as ex:
                        template = "Exception of type {0} occurred. Ignoring, pausing, then retrying:\n{1!r}"
                        message = template.format(type(ex).__name__, ex.args)
                        print(message)
                        tm.sleep(5)
                        continue
                    break

                # list tracks, we'll assume no album will exceed 100 tracks for now
                my_tracks = [track['uri'] for track in album['tracks']['items']]
                api_result = None
                # while True:
                #     try:
                #         # we'll do one API call per album, hitting the API a bit less than track per track
                #         api_result = settings.spotify.audio_features(my_tracks)
                #     except Exception as ex:
                #         template = "Exception of type {0} occurred. Ignoring, pausing, then retrying:\n{1!r}"
                #         message = template.format(type(ex).__name__, ex.args)
                #         print(message)
                #         tm.sleep(5)
                #         continue
                #     break
                try:
                    api_result = get_audio_features_with_retry(my_tracks)
                    # Process the audio features here
                except Exception as e:
                    print(f"Failed to retrieve audio features: {e}")

                track_number = 0
                for track_feature in api_result:
                    tempo = 0
                    my_track_name = album['tracks']['items'][track_number]['name']
                    # Even when fetched from API, details are not guaranteed to be available
                    if track_feature is not None:
                        tempo = round(track_feature['tempo'])

                    if settings.bpm_floor <= tempo <= settings.bpm_ceiling or \
                            settings.allow_doubled_bpm and settings.bpm_floor * 2 <= tempo <= settings.bpm_ceiling * 2:
                        print('  √ MATCH --> ♯', my_track_name, 'is', tempo, 'BPM')
                        # adding track to table if unique URI AND track name was not already added with another URI
                        cur.execute("INSERT OR IGNORE INTO t_tracks (track_uri, track_name, track_bpm)"
                                    "SELECT ?, ?, ?"
                                    "WHERE NOT EXISTS (SELECT * FROM t_tracks WHERE track_name = ?);",
                                    (track_feature['uri'], my_track_name, tempo
                                     if settings.bpm_floor <= tempo <= settings.bpm_ceiling
                                     else tempo / 2, my_track_name))
                        match_found = True
                    track_number += 1
    db_result = cur.execute("SELECT COUNT(*) AS count_of_tracks "
                            "FROM   t_tracks t "
                            "WHERE NOT EXISTS (SELECT 1 FROM t_tracks_in_playlists tp "
                            "                  WHERE t.ROWID = tp.track_id)")
    print(f'≡ Detected {db_result.fetchone()[0]} tracks matching search criteria.')

    settings.sql.commit()
    print("")
    print("=-" * 40)
    print("")
    if match_found:
        # get playlist and track information from user
        load_playlists(settings=settings)
        my_playlist_uri = ""
        # get a first valid target playlist to save into
        while True:
            my_playlist_uri, my_playlist_name = get_next_target_playlist(settings=settings,
                                                                         target_playlist=settings.target_playlist,
                                                                         bookmark_uri=my_playlist_uri)
            if settings.max_tracks_per_playlist:  # only a max-setting of 0 would be falsy
                tracks_saved = count_tracks_in_playlist(settings=settings, playlist_uri=my_playlist_uri)
                if tracks_saved < settings.max_tracks_per_playlist:
                    break
                else:
                    if settings.debug:
                        print(f'☼ Skipping playlist {my_playlist_uri} as it has {tracks_saved} tracks already '
                              f'and max_tracks_per_playlist is {settings.max_tracks_per_playlist}.')
            else:
                break

        # loop over all found tracks and attach to playlist(s) except if those same tracks were already added before
        my_api_limit = 95  # The add tracks to playlist API allows maximum 100 tracks at a time
        my_tracks = []
        query = """
        SELECT t.track_uri FROM t_tracks t 
        WHERE NOT EXISTS (SELECT 1 FROM t_tracks_in_playlists p 
                          WHERE t.ROWID = p.track_id LIMIT 1) 
        ORDER BY track_bpm ASC, track_order ASC;
        """
        # we're fetching the full list because database manipulations inside the loop
        # seemed to have a tendency to break the cursor off
        # this comes at a memory cost but acceptably so
        rows = cur.execute(query).fetchall()
        print(f'Of the ≡ detected tracks, {len(rows)} are new ones.')

        track_count_overall = 0
        track_count_in_playlist = 0
        decision_save = False
        decision_stop = False
        decision_next_playlist = False
        for row in rows:
            # print(row)
            my_tracks.append(row[0])
            track_count_overall += 1
            track_count_in_playlist += 1
            # decision 1: check if global maximum tracks to be saved has been attained
            if track_count_overall == settings.max_tracks_to_save:
                decision_save = True
                decision_stop = True
                if settings.debug:
                    print(f'☼ Saving {len(my_tracks)} tracks to playlist {my_playlist_uri} '
                          f'as max_tracks_to_save {track_count_overall} reached, then stopping.')
            else:
                # decision 2: check if max_tracks_per_playlist has been reached for this playlist
                if settings.max_tracks_per_playlist and \
                        track_count_in_playlist + tracks_saved >= settings.max_tracks_per_playlist:
                    decision_save = True
                    decision_next_playlist = True
                    if settings.debug:
                        print(f'☼ Saving {len(my_tracks)} tracks to playlist {my_playlist_uri} '
                              f'as max_tracks_per_playlist {settings.max_tracks_per_playlist} reached, '
                              f'then switching to next playlist.')
                else:
                    # decision 3: check if API call payload has been maxed out
                    # note how track_count_overall keeps incrementing, while len(my_tracks) gets reset after every save.
                    if len(my_tracks) == my_api_limit:
                        decision_save = True
                        if settings.debug:
                            print(f'☼ Saving a batch of {len(my_tracks)} tracks to playlist {my_playlist_uri} '
                                  f'as API limit {my_api_limit} reached.')
            # perform actions according to decision tree
            if decision_save:
                add_playlist_tracks(settings=settings, playlist_uri=my_playlist_uri, playlist_name=my_playlist_name,
                                    tracks=my_tracks)
                my_tracks = []
                decision_save = False

            if decision_next_playlist:
                # get a consecutive target playlist to save into
                tracks_saved = settings.max_tracks_per_playlist
                while tracks_saved >= settings.max_tracks_per_playlist:
                    my_playlist_uri, my_playlist_name = \
                        get_next_target_playlist(settings=settings, target_playlist=settings.target_playlist,
                                                 bookmark_uri=my_playlist_uri)

                    # go count how many tracks already present in the current target playlist,
                    # if it already has too many tracks, get a next playlist.
                    tracks_saved = count_tracks_in_playlist(settings=settings, playlist_uri=my_playlist_uri)
                decision_next_playlist = False
                track_count_in_playlist = 0

            if decision_stop:
                break
        else:  # this runs when the for loop on tracks reached EOF
            if my_tracks:  # flush remainder, if any
                add_playlist_tracks(settings=settings, playlist_uri=my_playlist_uri, playlist_name=my_playlist_name,
                                    tracks=my_tracks)

    else:
        print('⚠ There were no tracks found for the search criteria: ')
        print('|    ┕■ genre="', settings.genre_searchstring, '"')
        print('|    ┕■ artist="', settings.artist_searchstring, '"')

    settings.sql_cursor.close()
    print('┕■ Spotify Run List maker completed at ', datetime.now())


def get_audio_features_with_retry(track_uris, max_retries=5, initial_delay=1.0):
    retries = 0
    delay = initial_delay

    while retries < max_retries:
        try:
            # Attempt to fetch audio features
            # we'll do one API call per album, hitting the API a bit less than track per track
            api_result = settings.spotify.audio_features(track_uris)
            return api_result  # Return the result if successful
        except settings.spotify.exceptions.SpotifyException as ex:
            if ex.http_status == 429:
                # If rate limit exceeded, extract Retry-After header value (in seconds)
                retry_after = int(ex.headers.get("Retry-After", delay)) + 5
                print(f"Rate limit exceeded. Retrying after {retry_after} seconds.")
                tm.sleep(retry_after)
            else:
                print(f"SpotifyException occurred while fetching audio features: {ex}")
                tm.sleep(delay)
        except Exception as ex:
            print(f"Unexpected exception occurred while fetching audio features: {ex}")
            tm.sleep(delay)

        retries += 1
        delay *= 2  # Exponential backoff

    raise Exception(f"Failed to get audio features after {max_retries} retries.")


# Standard boilerplate to call the main() function to begin
# the program.
if __name__ == '__main__':
    main()
