
from rediz.client import Rediz
from rediz.collider_config_private import REDIZ_COLLIDER_CONFIG
from pprint import pprint


SURE = True


if __name__ == '__main__':
    rdz     = Rediz(**REDIZ_COLLIDER_CONFIG)
    ownership = rdz.client.hgetall(rdz._OWNERSHIP)
    names_to_delete = ['r_'+str(i)+'.json' for i in range(5000)]

    if SURE:
        for name in names_to_delete:
            resultz = rdz._delete_implementation(names=names_to_delete)

