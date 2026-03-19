from sites.club_spark.site import ClubSparkSite
from sites.raynes_park.site import RaynesParkSite


def build_registry() -> dict[str, type]:
    return {
        ClubSparkSite.name: ClubSparkSite,
        RaynesParkSite.name: RaynesParkSite,
    }
