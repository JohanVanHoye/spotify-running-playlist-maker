import time as tm


def load_playlists(settings):
    """
    Loads all playlists the current user has into in-memory table with some attributes
    that show whether these playlists are for exclusion or target playlists.

    :return: adds records in in-memory table
    """
    user = settings.spotify.me()["id"]
    print(f'▸ Retrieving {settings.spotify.me()["display_name"]}\'s Playlists ...')
    my_limit = 50
    my_offset = 0
    my_count = 0
    db = settings.sql_cursor
    # Find all of our user's playlists, consecutive searches may be needed
    while my_offset == 0 or api_playlists['next']:
        api_playlists = settings.spotify.user_playlists(user=user, limit=my_limit, offset=my_offset)
        for searchResult in api_playlists['items']:
            # check if this is a playlist we want to ignore tracks from
            if searchResult["name"] in settings.playlists_to_exclude:
                is_marked_for_exclusion = True
            else:
                is_marked_for_exclusion = False
            # check if this playlist matches our target playlist name
            is_target = searchResult["name"].startswith(settings.target_playlist)
            # store
            db.execute("INSERT OR IGNORE INTO t_playlists "
                       "(playlist_uri, playlist_name, is_marked_for_exclusion, is_target) SELECT ?, ?, ?, ?",
                       (searchResult["uri"], searchResult["name"], is_marked_for_exclusion, is_target))
            if is_marked_for_exclusion or is_target:
                # then we also need its tracks
                load_playlist_tracks(settings=settings, playlist_uri=searchResult["uri"])
            my_count += 1
        my_offset += my_limit
    print('ᴥ User has', my_count, '▸ playlists.')
    # for row in db.execute("select * from t_playlists"):
    #     print(row)


def load_playlist_tracks(settings, playlist_uri):
    """
    Loads the tracks for a given playlist for further exclusion.

    a) Because Spotify's Web API does not natively offer a way to check that you are not adding
    duplicate tracks to the same playlist, we pull the existing track ID's on the playlist into a table,
    so we can exclude them from being added again later.

    b) We also use this same method to later check we are not adding tracks from other, user-supplied playlists.

    :param settings: a configuration object containing program settings, sql context and spotipy client
    :param playlist_uri: URI of the playlist you want to fetch tracks of

    :return: adds records in in-memory table
    """
    my_limit = 50
    my_offset = 0
    api_result = None
    db = settings.sql_cursor
    while my_offset == 0 or api_result['next']:
        api_result = settings.spotify.playlist_items(playlist_id=playlist_uri,
                                                     fields='next,items(track(uri,name))',
                                                     limit=my_limit, offset=my_offset)
        for searchResult in api_result['items']:
            my_track_uri = searchResult['track']['uri']
            my_track_name = searchResult['track']['name']

            # adding track to table of tracks
            db.execute("INSERT OR IGNORE INTO t_tracks (track_uri, track_name) "
                       "SELECT ?, ?",
                       (my_track_uri, my_track_name))
            # linking track to playlist
            db.execute("INSERT OR IGNORE INTO t_tracks_in_playlists (playlist_id, track_id) "
                       "SELECT (SELECT ROWID from t_playlists WHERE playlist_uri = ?),"
                       "       (SELECT ROWID FROM t_tracks WHERE track_uri = ?)",
                       (playlist_uri, my_track_uri))
        my_offset += my_limit
    # query = "SELECT * FROM t_tracks_in_playlists;"
    # print(query)
    # print('')
    # for row in cur.execute(query):
    #     print(row)


def create_playlist(settings, playlist_name, playlist_description):
    """
    Creates a new playlist in Spotify (and in in-memory database).

    :param settings: a configuration object containing program settings, sql context and spotipy client
    :param playlist_name: name of new playlist
    :param playlist_description: the playlist's description
    :return: URI of the new playlist
    """
    db = settings.sql_cursor
    user = settings.spotify.me()["id"]
    print(f'▸ Creating Playlist "{playlist_name}"...')
    response = settings.spotify.user_playlist_create(
        user=user, name=playlist_name, public=False, collaborative=False,
        description=playlist_description)
    db.execute("INSERT OR IGNORE INTO t_playlists (playlist_uri, playlist_name, is_marked_for_exclusion, is_target) "
               "SELECT ?, ?, ?, ?",
               (response["uri"], playlist_name, False, True))
    # Since this is a new playlist it has to be a "target" playlist and will have no tracks associated to it yet.
    return response["uri"]


def count_tracks_in_playlist(settings, playlist_uri):
    """
    Counts how many tracks there are in a given playlist.
    :param settings: a configuration object containing program settings, sql context and spotipy client
    :param playlist_uri: URI of the playlist you want to fetch tracks of
    :return: count of tracks in playlist, a non-negative integer value.
    """
    db = settings.sql_cursor
    db_result = db.execute("SELECT COUNT(*) AS count_of_tracks_in_playlist "
                           "FROM   t_tracks_in_playlists tp "
                           "JOIN   t_playlists p "
                           "ON     p.ROWID = tp.playlist_id "
                           "WHERE  p.playlist_uri = ? ", (playlist_uri,))
    return db_result.fetchone()[0]


def get_next_target_playlist(settings, target_playlist, bookmark_uri):
    """
    get next available target playlist, making a new one if none left
    :param settings: a configuration object containing program settings, sql context and spotipy client
    :param target_playlist: name of playlist
    :param bookmark_uri: URI of the playlist acting as a bookmark
    :return: uri of next existing or new playlist.
    """
    db = settings.sql_cursor
    number_of_playlists = 0
    bookmark_found = False
    # determine list of candidate playlists
    for row in db.execute("SELECT 0 AS new, playlist_uri, playlist_name "
                          "FROM  t_playlists p "
                          "WHERE is_target = true "
                          "UNION SELECT 1 AS new, null AS playlist_uri, null AS playlist_name "
                          "ORDER BY new ASC, playlist_name ASC "):
        # print(row)
        my_playlist_uri = row[1]
        my_playlist_name = row[2]
        # if we didn't find or ran out of suitable playlists, then create a next one
        if my_playlist_uri is None:
            # create new playlist
            my_playlist_name = f'{target_playlist}' \
                          + (f' part {"{:02d}".format(number_of_playlists)}' if number_of_playlists > 0 else '')
            my_desc = f'Generated playlist with {settings.genre_searchstring} tracks in tempo between ' \
                      f'{settings.bpm_floor} and {settings.bpm_ceiling} beats per minute.'
            my_playlist_uri = create_playlist(settings=settings, playlist_name=my_playlist_name,
                                              playlist_description=my_desc)
            if settings.debug:
                print(f'☼ Created new playlist "{my_playlist_name}" with URI {my_playlist_uri}.')

        # print(my_playlist_uri, type(my_playlist_uri))
        else:
            number_of_playlists += 1
            # test: is this the bookmarked one?
            if my_playlist_uri == bookmark_uri:
                bookmark_found = True
            # if there wasn't a bookmark provided, yet we did find a playlist, then stick with that one.
            if bookmark_uri == "":
                break
        # only break out of loop when the first next playlist after bookmark was found
        if bookmark_found and my_playlist_uri != bookmark_uri:
            if settings.debug:
                print(f'☼ Changing context to existing playlist "{my_playlist_name}" with URI {my_playlist_uri}.')
            break

    return my_playlist_uri, my_playlist_name


def add_playlist_tracks(settings, playlist_uri, playlist_name, tracks):
    """
    Adds tracks to designated playlist.
    :param settings: a configuration object containing program settings, sql context and spotipy client
    :param playlist_uri: URI of the playlist to add tracks to
    :param playlist_name: name of the playlist, for console logging
    :param tracks: list of track URIs to save
    :return: None, only adds tracks to playlist.
    """
    try:
        settings.spotify.playlist_add_items(playlist_id=playlist_uri, items=tracks)
    except Exception as ex:
        template = "Exception of type {0} occurred. Ignoring, pausing, then retrying:\n{1!r}"
        message = template.format(type(ex).__name__, ex.args)
        print(message)
        print('with:', playlist_uri, tracks)
        tm.sleep(5)

    # sync in-memory representation, too
    load_playlist_tracks(settings=settings, playlist_uri=playlist_uri)

    print(f'  ┕ {len(tracks)} tracks successfully added to playlist {playlist_name}.')
