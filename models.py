class Artist:
    def __init__(self, name, uri):
        self.name = str(name)
        self.uri = str(uri)

    def __str__(self):
        return self.name


class Playlist:
    def __init__(self, name, uri):
        self.name = str(name)
        self.uri = str(uri)

    def __str__(self):
        return self.name


class Track:
    def __init__(self, name, uri):
        self.name = str(name)
        self.uri = str(uri)

    def __str__(self):
        return self.name
