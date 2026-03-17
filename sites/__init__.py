from sites.raynes_park.site import RaynesParkSite


def build_registry() -> dict[str, type[RaynesParkSite]]:
    return {RaynesParkSite.name: RaynesParkSite}
