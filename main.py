import time as tm
from datetime import datetime
from models import Artist
from settings import Settings
from playlists import load_playlists, count_tracks_in_playlist, get_next_target_playlist, add_playlist_tracks


# load and validate user settings from toml file
# this object also contains SQL database engine and Spotipy client.
settings = Settings()


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
    artists_to_process = set()
    my_limit = 50
    my_offset = 0
    api_result = None
    match_found = False

    while my_offset == 0 or (api_result['artists']['next'] and my_offset < 1000):
        api_result = settings.spotify.search(f'genre:"{settings.genre_searchstring}" {settings.artist_searchstring}',
                                             type='artist', limit=my_limit, offset=my_offset)
        for searchResult in api_result['artists']['items']:
            artists_to_process.add(Artist(searchResult["name"], searchResult["uri"]))
        my_offset += my_limit

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
            results = settings.spotify.artist_albums(artist.uri, album_type='album')
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
                                     if settings.bpm_floor <= tempo <= settings.bpm_ceiling else tempo / 2, my_track_name))
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
        print('⚠ There were no tracks found for the search criteria', settings.genre_searchstring, '&',
              settings.artist_searchstring)

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
