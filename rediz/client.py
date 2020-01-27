from itertools import zip_longest
import fakeredis, os, re, sys, uuid, math, json, redis, time
import threezaconventions.crypto
from collections import OrderedDict
from typing import List, Union, Any, Optional
from redis.client import list_or_args

# REDIZ
# -----
# Implements a write-permissioned (not read permissioned) shared REDIS value store with subscription, history
# and delay mechanisms. Intended for collectivized short term (e.g. 5 seconds or 15 minutes) prediction.
#
# The usage pattern is a sequence of scheduled calls:
#     my_secret_key="eae775f3-a33a-4105-ab8f-77336b0a3921"
#     while True:
#         time.sleep(60)
#         measurement = measure_somehow()
#         set(name='air-pressure-06820.json',write_key=my_secret_key,value=measurement)         [ assumes name not taken yet ]
#
# Then later:
#     one_minute_forecast     = get(name='predicted:1:air-pressure-06820.json')
#     fifteen_minute_forecast = get(name='predicted:900:air-pressure-06820.json')
#
# Public methods
# --------------
#   > set, get, delete
#   > subscribe, unsubscribe
#
# Administrative methods
# ----------------------
#   > admin_garbage_collection         ... should be run every fifteen minutes, say
#   > admin_promises                   ... should be run every minute
#
# Permissioning
# -------------
# Permissioning is achieved by a hash of name->official_write_key. It is first-in-best dressed, but ownership will
# be relinquished if the value expires. The time to live is determined by the memory size of the value, and is refreshed
# every time set() is called.
#
# Implementation notes:
# --------------------
# There are hooks built into set() that propagate new values to history, delay queues and subscriber mailboxes.
# An instruction to copy a value from name to delay:5:name will sit in a set of promises until either admin_promises()
# is called or the set expires. Expiry typically occurs 65 seconds after the scheduled time of the copy.
# Periodic calls to _garbage_collection() will stochastically remove artifacts of expired data.
# All commands accept list arguments and use pipelining to minimize communication with the server. Over time some will
# be moved into lua scripts to further reduce the number of calls made.

PY_REDIS_ARGS = ('host','port','db','username','password','socket_timeout','socket_keepalive','socket_keepalive_options',
                 'connection_pool', 'unix_socket_path','encoding', 'encoding_errors', 'charset', 'errors',
                 'decode_responses', 'retry_on_timeout','ssl', 'ssl_keyfile', 'ssl_certfile','ssl_cert_reqs', 'ssl_ca_certs',
                 'ssl_check_hostname', 'max_connections', 'single_connection_client','health_check_interval', 'client_name')
FAKE_REDIS_ARGS = ('decode_responses',)

KeyList   = List[Optional[str]]
NameList  = List[Optional[str]]
ValueList = List[Optional[Any]]

DEBUGGING = True
def dump(obj,name="client.json"):
    if DEBUGGING:
        json.dump(obj,open("tmp_client.json","w"))



class Rediz(object):

    def __init__(self,**kwargs):
        self.client         = self.make_redis_client(**kwargs)         # Real or mock redis client
        # Reserved redis keys and prefixes
        self._obscurity      = "09909e88-ca04-4868-a0a0-c94748df844f:::"
        self.OWNERSHIP      = self._obscurity+"ownership"
        self.NAMES          = self._obscurity+"names"
        self.PROMISES       = self._obscurity+"promises:"
        # User facing conventions
        self.DELAYED        = "delayed::"
        self.MESSAGES       = "messages::"
        self.HISTORY        = "history::"
        self.SUBSCRIBERS    = "subscribers::"
        self.DELAY_SECONDS  = kwargs.get("delay_seconds")  or [5,10,30,60,1*60,2*60,5*60,10*60,20*60,60*60]
        #
        self.RESERVED       = [self.OWNERSHIP, self.NAMES, self.PROMISES, self.MESSAGES, self.HISTORY, self.SUBSCRIBERS, self.DELAY_SECONDS, self._obscurity]

    @staticmethod
    def is_valid_name(name:str):
        name_regex = re.compile(r'^[-a-zA-Z0-9_.:]{1,200}\.[json,HTML]+$',re.IGNORECASE)
        return (re.match(name_regex, name) is not None) and (not '::' in name)

    @staticmethod
    def assert_not_in_reserved_namespace(names, *args):
        names = list_or_args(names,args)
        if any( "::" in name for name in names ):
            raise Exception("Operation attempted with a name that lies in reserved namespace (e.g. double colon).")

    @staticmethod
    def is_valid_value(value):
        return sys.getsizeof(value)<100000

    @staticmethod
    def is_valid_key(key):
        return isinstance(key,str) and len(key)==len(str(uuid.uuid4()))

    @staticmethod
    def random_key():
        return threezaconventions.crypto.random_key()

    @staticmethod
    def random_name():
        return threezaconventions.crypto.random_key()+'.json'

    def get(self,name:Optional[str]=None,
                 names:Optional[NameList]=None, **nuissance ):
        """ Retrieve value(s). There is no permissioning on read """
        names = names or [ name ]
        res = self._pipelined_get(names=names)
        return res if (name is None) else res[0]

    def mget(self, names:NameList, *args):
        """ Redundant but in keeping with redis naming tradition """
        names = list_or_args(names,args)
        return self.get(names=names)

    def set(self,names:Optional[NameList]=None,
                 values:Optional[ValueList]=None,
                 write_keys:Optional[KeyList]=None,
                 name:Optional[str]=None,
                 value:Optional[Any]=None,
                 write_key:Optional[str]=None, **nuissance):
        """
                  :param
                  returns:  [ {"name":name, "write_key":write_key} ]  if names is supplied,
        otherwise returns:    {"name":name, "write_key":write_key}    when used in the singular
        """
        singular = names is None
        names, values, write_keys = self._coerce_inputs(names=names,values=values,
                                  write_keys=write_keys,name=name,value=value,write_key=write_key)
        # Execute
        execution_log = self._set( names=names,values=values, write_keys=write_keys )
        # Re-jigger results
        access = self._coerce_outputs( execution_log )

        return access[0] if singular else access

    def mset(self,names:Optional[NameList]=None,
                  values:Optional[ValueList]=None,
                  write_keys:Optional[KeyList]=None, **nuissance):
        """ Redundant but in keeping with redis naming conventions """
        return self.set(names=names, values=values, write_keys=write_keys )

    def _subscribe(self, publisher, subscriber ):
        self.client.sadd(self.SUBSCRIBERS+publisher,subscriber)

    def _unsubscribe(self, publisher, subscriber ):
        self.client.srem(self.SUBSCRIBERS+publisher,subsriber)

    def delete(self, name=None, write_key=None, names:Optional[NameList]=None, write_keys:Optional[KeyList]=None ):
        """ Permissioned delete """
        names, write_keys = names or [ name ], write_keys or [ write_key ]
        are_valid = self._are_valid_write_keys(names, write_keys)
        authorized_kill_list = [ name for (name,is_valid_write_key) in zip(names,are_valid) if is_valid_write_key ]
        self._delete(*authorized_kill_list)

    def admin_garbage_collection(self, fraction=0.01 ):
        """ Randomized search and destroy for expired data """
        num_keys     = self.client.scard(self.NAMES)
        num_survey   = min( 100, max( 20, int( fraction*num_keys ) ) )
        orphans      = self._randomly_find_orphans( num=num_survey )
        if orphans is not None:
            self._delete(*orphans)
            return len(orphans)
        else:
            return 0

    def _delete(self, names, *args ):
        """ Remove data, subscriptions, messages, ownership, history and set entry """
        names = list_or_args(names,args)
        self.assert_not_in_reserved_namespace(names)

        subs_pipe = self.client.pipeline()
        for name in names:
            subs_pipe.smembers(name=self.SUBSCRIBERS+name)
        subs_res = subs_pipe.execute()

        messages_removal_pipe = self.client.pipeline(transaction=False)
        for name, subscribers in zip(names,subs_res):
            for subscriber in subscribers:
                recipient_mailbox = self.MESSAGES+name
                messages_removal_pipe.hdel(recipient_mailbox,name)
        messages_removal_pipe.execute()

        delete_pipe = self.client.pipeline()
        delete_pipe.hdel(self.OWNERSHIP,*names)
        delete_pipe.delete( *[self.SUBSCRIBERS+name for name in names] )
        delete_pipe.delete( *[self.MESSAGES+name for name in names] )
        delete_pipe.delete( *names )
        delete_pipe.delete( *[self.HISTORY+name for name in names] )
        delete_pipe.srem( self.NAMES, *names )
        delete_pipe.execute()

    def _randomly_find_orphans(self,num=1000):
        NAMES = self.NAMES
        unique_random_names = list(set(self.client.srandmember(NAMES,num)))
        num_random = len(unique_random_names)
        if num_random:
            num_exists = self.client.exists(*unique_random_names)
            if num_exists<num_random:
                # There must be orphans, defined as those who are listed
                # in reserved["names"] but have expired
                exists_pipe = self.client.pipeline(transaction=True)
                for name in unique_random_names:
                    exists_pipe.exists(name)
                exists  = exists_pipe.execute()

                orphans = [ name for name,ex in zip(unique_random_names,exists) if not(ex) ]
                return orphans

    @staticmethod
    def _coerce_inputs(  names:Optional[NameList]=None,
                         values:Optional[ValueList]=None,
                         write_keys:Optional[KeyList]=None,
                         name:Optional[str]=None,
                         value:Optional[Any]=None,
                         write_key:Optional[str]=None):
        # Convert singletons to arrays, broadcasting as necessary
        names  = names or [ name ]
        values = values or [ value for _ in names ]
        write_keys = write_keys or [ write_key for _ in names ]
        return names, values, write_keys

    @staticmethod
    def _coerce_outputs( execution_log ):
        """ Convert to list of dicts containing names and write keys """
        executed = sorted(execution_log["executed"], key = lambda d: d['ndx'])
        return [ {"name":s["name"],"write_key":s["write_key"]} for s in executed ]

    def _set(self,names:Optional[NameList]=None,
                  values:Optional[ValueList]=None,
                  write_keys:Optional[KeyList]=None,
                  name:Optional[str]=None,
                  value:Optional[Any]=None,
                  write_key:Optional[str]=None, **nuisance):
        # Returns execution log format
        names, values, write_keys = self._coerce_inputs(names,values,write_keys,name,value,write_key)
        ndxs = list(range(len(names)))
        executed_obscure,  rejected_obscure,  ndxs, names, values, write_keys = self._pipelined_set_obscure(  ndxs, names, values, write_keys)
        executed_new,      rejected_new,      ndxs, names, values, write_keys = self._pipelined_set_new(      ndxs, names, values, write_keys)
        executed_existing, rejected_existing                                  = self._pipelined_set_existing( ndxs, names, values, write_keys)

        modified_names  = [ ex["name"] for ex in executed_existing ]
        modified_values = [ ex["value"] for ex in executed_existing ]
        self._propagate_to_subscribers( names = modified_names, values = modified_values )
        executed = executed_obscure+executed_new+executed_existing
        return {"executed":executed,
                "rejected":rejected_obscure+rejected_new+rejected_existing}

    def _pipelined_get(self,names):
        if len(names):
            get_pipe = self.client.pipeline(transaction=True)
            for name in names:
                get_pipe.get(name=name)
            return get_pipe.execute()

    def _pipelined_set_obscure(self, ndxs, names, values, write_keys):
        # Set values only if names were None. This prompts generation of a randomly chosen obscure name.
        executed      = list()
        rejected      = list()
        ignored_ndxs  = list()
        if ndxs:
            obscure_pipe  = self.client.pipeline(transaction=True)

            for ndx, name, value, write_key in zip( ndxs, names, values, write_keys):
                if not(self.is_valid_value(value)):
                    rejected.append({"ndx":ndx, "name":name,"value":value,"error":"invalid value of type "+type(value)+" was supplied"})
                else:
                    if (name is None):
                        if write_key is None:
                            write_key = self.random_key()
                        if not(self.is_valid_key(write_key)):
                            rejected.append({"ndx":ndx,"name":name,"write_key":write_key,"errror":"invalid write_key"})
                        else:
                            new_name = self.random_name()
                            obscure_pipe, intent = self._new_obscure_page(pipe=obscure_pipe,ndx=ndx, name=new_name,value=value, write_key=write_key)
                            executed.append(intent)
                    elif not(self.is_valid_name(name)):
                        rejected.append({"ndx":ndx, "name":name,"error":"invalid name"})
                    else:
                        ignored_ndxs.append(ndx)

            if len(executed):
                obscure_results = Rediz.pipe_results_grouper( results=obscure_pipe.execute(), n=len(executed) )
                for intent, res in zip(executed,obscure_results):
                    intent.update({"result":res})

        # Marshall residual. Return indexes, names, values and write_keys that are yet to be processed.
        names          = [ n for n,ndx in zip(names, ndxs)       if ndx in ignored_ndxs ]
        values         = [ v for v,ndx in zip(values, ndxs)      if ndx in ignored_ndxs ]
        write_keys     = [ w for w,ndx in zip(write_keys, ndxs)  if ndx in ignored_ndxs ]
        return executed, rejected, ignored_ndxs, names, values, write_keys

    def _pipelined_set_new(self,ndxs, names, values, write_keys):

        executed      = list()
        rejected      = list()
        ignored_ndxs  = list()

        if ndxs:
            exists_pipe = self.client.pipeline(transaction=False)
            for name in names:
                exists_pipe.hexists(name=self.OWNERSHIP,key=name)
            exists = exists_pipe.execute()

            new_pipe     = self.client.pipeline(transaction=False)
            for exist, ndx, name, value, write_key in zip( exists, ndxs, names, values, write_keys):
                if not(exist):
                    if write_key is None:
                        write_key = self.random_key()
                    if not(self.is_valid_key(write_key)):
                        rejected.append({"ndx":ndx,"name":name,"write_key":write_key,"errror":"invalid write_key"})
                    else:
                        new_pipe, intent = self._new_page(new_pipe,ndx=ndx, name=name,value=value,write_key=write_key)
                        executed.append(intent)
                else:
                    ignored_ndxs.append(ndx)

            if len(executed):
                new_results = Rediz.pipe_results_grouper( results= new_pipe.execute(), n=len(executed) )
                for intent, res in zip(executed,new_results):
                    intent.update({"result":res})

        # Yet to get to...
        names          = [ n for n,ndx in zip(names, ndxs)       if ndx in ignored_ndxs ]
        values         = [ v for v,ndx in zip(values, ndxs)      if ndx in ignored_ndxs ]
        write_keys     = [ w for w,ndx in zip(write_keys, ndxs)  if ndx in ignored_ndxs ]
        return executed, rejected, ignored_ndxs, names , values, write_keys

    def _are_valid_write_keys(self,names,write_keys):
        return [ k==k1 for (k,k1) in zip( write_keys, self._write_keys(names) ) ]

    def _write_keys(self,names):
        if names:
            return self.client.hmget(name=self.OWNERSHIP,keys=names)

    def _pipelined_set_existing(self,ndxs, names,values, write_keys):
        executed     = list()
        rejected     = list()
        if ndxs:
            modify_pipe = self.client.pipeline(transaction=False)
            official_write_keys = self._write_keys(names)
            for ndx,name, value, write_key, official_write_key in zip( ndxs, names, values, write_keys, official_write_keys ):
                if write_key==official_write_key:
                    modify_pipe, intent = self._modify_page(modify_pipe,ndx=ndx,name=name,value=value)
                    intent.update({"write_key":write_key})
                    executed.append(intent)
                else:
                    rejected.append({"name":name,"value":value,"write_key":write_key,"official_write_key_ends_in":official_write_key[-4:],
                    "error":"write_key does not match page_key on record"})

            if len(executed):
                modify_results = Rediz.pipe_results_grouper( results = modify_pipe.execute(), n=len(executed) )
                for intent, res in zip(executed,modify_results):
                    intent.update({"result":res})

        return executed, rejected

    def _propagate_to_subscribers(self,names,values):

        subscriber_pipe = self.client.pipeline(transaction=False)
        for name in names:
            subscriber_set_name = self.SUBSCRIBERS+name
            subs = subscriber_pipe.smembers(name=subscriber_set_name)
        subscribers_sets = subscriber_pipe.execute()

        propagate_pipe = self.client.pipeline(transaction=False)

        executed = list()
        for sender_name, value,subscribers_set in zip(names, values,subscribers_sets):
            for subscriber in subscribers_set:
                mailbox_name = self.MESSAGES+subscriber
                propagate_pipe.hset(name=mailbox_name,key=sender_name, value=value)
                executed.append({"mailbox_name":mailbox_name,"sender":sender_name,"value":value})

        if len(executed):
            propagation_results = Rediz.pipe_results_grouper( results = propagate_pipe.execute(), n=len(executed) ) # Overkill while there is 1 op
            for intent, res in zip(executed,propagation_results):
                intent.update({"result":res})

        return executed

    @staticmethod
    def cost_based_ttl(value):
        # Economic assumptions
        REPLICATION         = 3.                          # History, messsages
        DOLLAR              = 10000.                      # Credits per dollar
        COST_PER_MONTH_10MB = 1.*DOLLAR
        COST_PER_MONTH_1b   = COST_PER_MONTH_10MB/(10*1000*1000)
        SECONDS_PER_DAY     = 60.*60.*24.
        SECONDS_PER_MONTH   = SECONDS_PER_DAY*30.
        FIXED_COST_bytes    = 100                        # Overhead
        MAX_TTL_SECONDS     = int(SECONDS_PER_DAY*7)

        num_bytes = sys.getsizeof(value)
        credits_per_month = REPLICATION*(num_bytes+FIXED_COST_bytes)*COST_PER_MONTH_1b
        ttl_seconds = int( math.ceil( SECONDS_PER_MONTH / credits_per_month ) )
        ttl_seconds = min(ttl_seconds,MAX_TTL_SECONDS)
        ttl_days = ttl_seconds / SECONDS_PER_DAY
        return ttl_seconds, ttl_days

    @staticmethod
    def make_redis_client(**kwargs):
        kwargs["decode_responses"] = True   # Strong rediz convention
        is_real = "host" in kwargs          # May want to be explicit here
        KWARGS = PY_REDIS_ARGS if is_real else FAKE_REDIS_ARGS
        redis_kwargs = dict()
        for k in KWARGS:
            if k in kwargs:
                redis_kwargs[k]=kwargs[k]
        if is_real:
            return redis.StrictRedis(**redis_kwargs)
        else:
            return fakeredis.FakeStrictRedis(**redis_kwargs)



    @staticmethod
    def pipe_results_grouper(results,n):
        """ A utility for collecting pipelines where operations are in chunks """
        def grouper(iterable, n, fillvalue=None):
            args = [iter(iterable)] * n
            return zip_longest(*args, fillvalue=fillvalue)

        m = int(len(results)/n)
        return list(grouper(iterable=results,n=m,fillvalue=None))


    def _new_obscure_page( self, pipe, ndx, name, value, write_key):
        pipe, intent = self._new_page( pipe=pipe, ndx=ndx, name=name, value=value, write_key=write_key )
        intent.update({"obscure":True})
        return pipe, intent

    def _new_page( self, pipe, ndx, name, value, write_key ):
        """ Create new page
              pipe         :  Redis pipeline
              intent       :  Explanation in form of a dict
        """
        ttl, ttl_days = Rediz.cost_based_ttl(value)
        pipe.hset(name=self.OWNERSHIP,key=name,value=write_key)  # Establish ownership
        pipe.sadd(self.NAMES,name)                                # Need this for random access
        pipe, intent = self._modify_page(pipe,ndx=ndx,name=name,value=value)
        intent.update({"new":True,"write_key":write_key})
        return pipe, intent

    def _modify_page(self, pipe,ndx,name,value,intent=dict()):
        ttl, ttl_days = Rediz.cost_based_ttl(value)
        pipe.set(name=name,value=value,ex=ttl)
        # Also write a duplicate to another key
        name_of_copy   = self.random_key()+":"+name
        HISTORY_TTL = min( max( 2*60*60, ttl ), 60*60*24 )
        pipe.set(name=name_of_copy,value=value,ex=HISTORY_TTL)
        try:
            pipe.xadd(name=self.HISTORY+name,fields={"copy":name_of_copy})
        except:
            pass # Using fakeredis which doesn't yet support streams
        # Future copy operations
        import time
        utc_epoch_now = int(time.time())
        for delay_seconds in self.DELAY_SECONDS:
            PROMISE = self.PROMISES+str(utc_epoch_now+delay_seconds)
            SOURCE  = name_of_copy
            DESTINATION = self.DELAYED+str(delay_seconds)+":"+name
            pipe.sadd( PROMISE, SOURCE+'->'+DESTINATION )
            pipe.expire( name=PROMISE, time=delay_seconds+60)

        intent.update({"ndx":ndx,"name":name,"value":value,"ttl_days":ttl_days,
                    "new":False,"obscure":False,"copy":name_of_copy})

        return pipe, intent

    def admin_promises(self, lookback_seconds=65):
         exists_pipe = self.client.pipeline()
         utc_epoch_now = int(time.time())
         candidates =  [ self.PROMISES+str(utc_epoch_now-seconds) for seconds in range(lookback_seconds) ]
         for candidate in candidates:
             exists_pipe.exists(candidate)
         exists = exists_pipe.execute()

         get_pipe = self.client.pipeline()
         promise_collection_names = [ promise for promise,exist in zip(candidates,exists) if exists ]
         for collection_name in promise_collection_names:
             get_pipe.smembers(collection_name)
         collections = get_pipe.execute()
         self.client.delete( *promise_collection_names )  # Immediately delete task list so it isn't done twice ... not that that would
                                                          # be the end of the world
         import itertools
         individual_promises = list( itertools.chain( *collections ) )

         set_pipe = self.client.pipeline()
         sources  = list()
         destinations = list()
         for promise in individual_promises:
             dump(individual_promises[:5])
             try:
                 source, destination = promise.split('->')
             except:
                 source, destination = promise.split('>>')
             sources.append(source)
             destinations.append(destination)

         source_values = self.client.mget(*sources)
         mapping = dict ( zip(destinations, source_values ) )
         self.client.mset( mapping )
         return len(mapping)
