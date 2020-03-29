from rediz.client import Rediz
from rediz.collider_config_private import REDIZ_COLLIDER_CONFIG
import pprint

if __name__ == '__main__':
    rdz = Rediz(**REDIZ_COLLIDER_CONFIG)

    NAMES = ['three_body_x.json','bronx_traffic_speed_on_change.json',  'cop.json', 'z3~three_body_x~three_body_y~three_body_z~10810.json', 'z2~three_body_x~three_body_y~910.json', None]

    leaderboards = dict()
    for name in NAMES:
        leaderboards[name] = rdz.get_leaderboard(name=name,delay=rdz.delays[0],count=10)

    pprint.pprint(leaderboards)