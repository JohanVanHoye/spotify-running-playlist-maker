import spotipy
import sqlite3
from tomli import load as toml_load
from datetime import date
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth


class Settings:
    def __init__(self):
        self.genre_searchstring = ""

        # set configuration variables from settings
        with open("settings.toml", mode="rb") as fp:
            _config = toml_load(fp)

        self.genre_searchstring = _config["scanner"]["genre_searchstring"]
        self.artist_searchstring = _config["scanner"]["artist_searchstring"]
        self.interactive_mode = _config["scanner"]["interactive_mode"]
        self.bpm_floor: int = _config["filtering"]["bpm_floor"]
        self.bpm_ceiling = _config["filtering"]["bpm_ceiling"]
        self.allow_doubled_bpm = _config["filtering"]["allow_doubled_bpm"]
        self.playlists_to_exclude = _config["filtering"]["playlists_to_exclude"]
        self.max_tracks_per_playlist = _config["filtering"]["max_tracks_per_playlist"]
        self.max_tracks_to_save = _config["filtering"]["max_tracks_to_save"]
        self.spotipy_client_id = _config["spotify"]["spotipy_client_id"]
        self.spotipy_client_secret = _config["spotify"]["spotipy_client_secret"]
        self.spotipy_redirect_uri = _config["spotify"]["spotipy_redirect_uri"]
        self.debug = _config["application"]["debug"]
        # the target playlist name is 'derived' from settings ==>
        self.target_playlist = f'{self.genre_searchstring} @{self.bpm_floor}-{self.bpm_ceiling} BPM dd. {date.today()}'
        # check config values
        self.validate_settings()

        # provision an in-memory database
        self.sql = sqlite3.connect(":memory:")
        self.sql_cursor = self.sql.cursor()
        self.initialize_sql()

        # prepare spotipy spotify client
        self.spotify = spotipy.Spotify(client_credentials_manager=SpotifyClientCredentials(
            client_id=self.spotipy_client_id,
            client_secret=self.spotipy_client_secret),
            auth_manager=SpotifyOAuth(client_id=self.spotipy_client_id,
                                      client_secret=self.spotipy_client_secret,
                                      redirect_uri=self.spotipy_redirect_uri,
                                      scope='playlist-modify-private, playlist-modify-public,'
                                            'playlist-read-private, playlist-read-collaborative'))

    def validate_settings(self):
        # Some minimal validation of user-provided settings
        assert (isinstance(self.genre_searchstring, str)), \
            f'The "genre_searchstring" in settings.toml must be a string value ' \
            f'(is now {type(self.genre_searchstring).__name__}).'
        assert (len(self.genre_searchstring) > 0), \
            'The "genre_searchstring" setting cannot be left blank in settings.toml.'
        assert (isinstance(self.artist_searchstring, str)), \
            f'The "artist_searchstring" in settings.toml must be a string value ' \
            f'(is now {type(self.artist_searchstring).__name__}).'
        assert (isinstance(self.interactive_mode, bool)), \
            f'The "interactive_mode" in settings.toml must be set to either true or false, lowercase ' \
            f'(is now {type(self.interactive_mode).__name__}).'
        assert (isinstance(self.bpm_floor, int)), \
            f'The "bpm_floor" in settings.toml must be an integer value (is now {type(self.bpm_floor).__name__}).'
        assert (self.bpm_floor >= 0), \
            f'The "bpm_floor" in settings.toml must be a non-negative integer value (is now {self.bpm_floor}).'
        assert (isinstance(self.bpm_ceiling, int)), \
            f'The "bpm_ceiling" in settings.toml must be an integer value (is now {type(self.bpm_ceiling).__name__}).'
        assert (self.bpm_ceiling >= 0), \
            f'The "bpm_ceiling" in settings.toml must be a non-negative integer value (is now {self.bpm_ceiling}).'
        assert (self.bpm_ceiling >= self.bpm_floor), \
            f'The "bpm_ceiling" value ({self.bpm_ceiling}) in settings.toml ' \
            f'cannot be slower than the "bpm_floor" value ({self.bpm_floor}).'
        assert (isinstance(self.allow_doubled_bpm, bool)), \
            f'The "allow_doubled_bpm" in settings.toml must be set to either true or false, lowercase ' \
            f'(is now {type(self.allow_doubled_bpm).__name__}).'
        assert (isinstance(self.playlists_to_exclude, list)), \
            f'The "playlists_to_exclude" in settings.toml must be a list ' \
            f'(is now {type(self.playlists_to_exclude).__name__}).'
        assert (isinstance(self.max_tracks_per_playlist, int)), \
            f'The "max_tracks_per_playlist" in settings.toml must be an integer value ' \
            f'(is now {type(self.max_tracks_per_playlist).__name__}).'
        assert (self.max_tracks_per_playlist >= 0), \
            f'The "max_tracks_per_playlist" in settings.toml must be a non-negative integer value ' \
            f'(is now {self.max_tracks_per_playlist}).'
        assert (isinstance(self.max_tracks_to_save, int)), \
            f'The "max_tracks_to_save" in settings.toml must be an integer value ' \
            f'(is now {type(self.max_tracks_to_save).__name__}).'
        assert (self.max_tracks_to_save >= 0), \
            f'The "max_tracks_to_save" in settings.toml must be a non-negative integer value ' \
            f'(is now {self.max_tracks_to_save}).'
        assert (isinstance(self.spotipy_client_id, str)), \
            f'The "spotipy_client_id" in settings.toml must be a string value ' \
            f'(is now {type(self.spotipy_client_id).__name__}).'
        assert (len(self.spotipy_client_id) > 0), \
            'The "spotipy_client_id" setting cannot be left blank in settings.toml.'
        assert (len(self.spotipy_client_id) > 5), \
            f'The "spotipy_client_id" value "{self.spotipy_client_id}" in settings.toml is probably too short.'
        assert (isinstance(self.spotipy_client_secret, str)), \
            f'The "spotipy_client_secret" in settings.toml must be a string value ' \
            f'(is now {type(self.spotipy_client_secret).__name__}).'
        assert (len(self.spotipy_client_secret) > 0), \
            'The "spotipy_client_secret" setting cannot be left blank in settings.toml.'
        assert (len(self.spotipy_client_secret) > 5), \
            f'The "spotipy_client_secret" value "{self.spotipy_client_secret}" in settings.toml is probably too short.'
        assert (isinstance(self.spotipy_redirect_uri, str)), \
            f'The "spotipy_redirect_uri" in settings.toml must be a string value ' \
            f'(is now {type(self.spotipy_redirect_uri).__name__}).'
        assert (len(self.spotipy_redirect_uri) > 0), \
            'The "spotipy_redirect_uri" setting cannot be left blank in settings.toml.'
        assert (len(self.spotipy_redirect_uri) > 8), \
            f'The "spotipy_redirect_uri" value "{self.spotipy_redirect_uri}" in settings.toml is probably too short.'
        assert (isinstance(self.debug, bool)), \
            f'The "debug" in settings.toml must be set to either true or false, lowercase ' \
            f'(is now {type(self.debug).__name__}).'

    def initialize_sql(self):
        self.sql_cursor.execute("CREATE TABLE t_genres ("
                                "ROWID INTEGER PRIMARY KEY, "
                                "genre_name TEXT UNIQUE NOT NULL, "
                                "genre_count INT NOT NULL, "
                                "is_processed BOOLEAN NOT NULL);")
        self.sql_cursor.execute("CREATE TABLE t_playlists ("
                                "ROWID INTEGER PRIMARY KEY, "
                                "playlist_uri TEXT UNIQUE NOT NULL, "
                                "playlist_name TEXT UNIQUE NOT NULL, "
                                "is_marked_for_exclusion BOOLEAN NOT NULL, "
                                "is_target BOOLEAN NOT NULL);")
        self.sql_cursor.execute("CREATE TABLE t_tracks ("
                                "ROWID INTEGER PRIMARY KEY, "
                                "track_uri TEXT UNIQUE NOT NULL, "
                                "track_name TEXT UNIQUE NOT NULL, "
                                "track_bpm INT NULL, "
                                "track_order INT NOT NULL DEFAULT (random()));")
        self.sql_cursor.execute("CREATE TABLE t_tracks_in_playlists ("
                                "ROWID INTEGER PRIMARY KEY, "
                                "playlist_id INTEGER NOT NULL, "
                                "track_id INTEGER NOT NULL);")
