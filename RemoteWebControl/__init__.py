# RemoteWebControl plugin entry point.
#
# Cura-dependent modules are imported lazily inside register() so that the pure
# modules of this package (config, server, coords, ...) can be imported and tested
# with pytest outside Cura.


def getMetaData():
    return {}


def register(app):
    from .RemoteWebControlPlugin import RemoteWebControlPlugin
    return {"extension": RemoteWebControlPlugin(app)}
