#!/usr/bin/env python

## TODO: expand the forest to use <slot>, <info>, <more>
## TODO: add <value> browser
## TODO: add <value>/<slot> browser.
## TODO: add <array>/<index> browser.
## TODO: add close button to popouts
## TODO: add static class list

import andbug, os.path, cgi, json, subprocess, threading
from urllib2 import quote as urlquote
import re

try:
    import bottle
except ImportError:
    raise andbug.DependencyError('navi requires the "bottle" package')
    
################################################################### UTILITIES
# These functions make life a little easier, doing things like restructuring
# data structures to be easier to use from templates.
#############################################################################

def index_seq(seq):
    for i in range(len(seq)):
        yield i, seq[i]

def get_threads():
    threads = proc.threads()[:] # TODO This workaround for view is vulgar.
    def tin(name):
        try:
            return int(re.split('<|>', name)[1])
        except:
            return name

    threads.sort(lambda a, b: cmp(tin(a.name), tin(b.name)))
    return threads

def get_classes():
    classes = proc.classes()[:] # TODO This workaround for view is vulgar.
    classes.sort(lambda a, b: cmp(a.jni, b.jni))

############################################################## INFO UTILITIES
# These functions summarize various Java objects into human-readable 
# representations.
#############################################################################

def thread_info(thread):
    info = str(thread)
    return info[7:] if info.startswith('thread ') else info

def frame_info(frame):
    info = str(frame).split( ', at ', 1)
    return info[0 if (len(info) == 1) else 1]

def truncate_ojni(jni):
    if jni.startswith('['):
        return truncate_ojni(jni[1:]) + '[]'

    if jni.startswith('L'): 
        jni = jni[1:]
        if jni.endswith(';'): jni = jni[:-1]

    jni = jni.split('/')
    if len(jni) == 1:
        return jni[0]
    else:
        return '%s.%s' % (
            '.'.join((a[0] if a else '') for a in jni[:-1]),
            jni[-1]
        )

def object_info(object):
    return '<%s>' % truncate_ojni(object.jni)

def info(value):
    if isinstance(value, andbug.Thread):
        return thread_info(value)
    if isinstance(value, andbug.Frame):
        return frame_info(value)
    if isinstance(value, andbug.Array):
        if value.jni in ('[C', '[B'):
            return repr(value).replace('\\x00', '') # HACK
    if isinstance(value, andbug.Object):
        return object_info(value)
    return str(value)
    
############################################################## VIEW UTILITIES
# These functions summarize various Java objects into JSON views suitable for
# navigation panels.  Each view comes as a list, consisting of the name of a
# suitable constructor, and a series of arguments for the constructor.
#############################################################################

def sequence_view(value):
    seq = ['seq', value.jni]
    for val in value:
        seq.append(info(val))
    return seq
    #TODO: slots

def object_view(value):
    seq = ['obj', value.jni]
    for key, val in value.fields.iteritems():
        seq.append((key, info(val), key))
    return seq
    #TODO: slots

def view(value):
    if isinstance(value, andbug.Array):
        return sequence_view(value)
    if isinstance(value, andbug.Object):
        return object_view(value)
    return ['val', info(value)]

################################################################## DATA ROOTS
# We use static roots derived from the location of the Navi script.
#############################################################################

NAVI_ROOT = os.path.abspath( 
    os.path.join( os.path.dirname( __file__ ), '..' )
)
STATIC_ROOT = os.path.join( NAVI_ROOT, 'data', '' )
COFFEE_ROOT = os.path.join( NAVI_ROOT, 'coffee', '' )
bottle.TEMPLATE_PATH.append( os.path.join( NAVI_ROOT, 'view' ) )

def resolve_resource(root, rsrc):
    assert root.endswith(os.path.sep)
    rsrc = os.path.abspath(root + rsrc)
    if rsrc.startswith(root):
        return rsrc
    else:
        raise Exception('Less dots next time.')

def resolve_coffee_resource(root, rsrc):
    # We do not cache this at all; since this is a single-user, one-page
    # interface.. I give a damn.
    
    return data

@bottle.route( '/s/:req#.*#' )
def static_data(req):
    rsrc = resolve_resource(COFFEE_ROOT, req)  

    if rsrc.endswith('.coffee') and os.path.exists(rsrc):
        req = rsrc.replace(COFFEE_ROOT, '')[:-7] + '.js'
        try:
            ret = subprocess.call(('coffee', '-o', STATIC_ROOT, '-c', rsrc))
        except OSError:
            pass # use the cached version, looks like coffee isn't working.
    return bottle.static_file(req, root=STATIC_ROOT)

################################################################# GLOBAL DATA
# Our Bottle server uses WSGIRef, which is a single-process asynchronous HTTP
# server.  Any given request handler can be sure that it has complete control
# of these globals, because WSGIRef is far too stupid to handle multiple
# concurrent requests.
#############################################################################

NAVI_VERNO = '0.1'
NAVI_VERSION = 'AndBug Navi ' + NAVI_VERNO

################################################################# THREAD AXIS
# The thread axis works from the process's thread list, digging into 
# individual thread frames and their associated slots.
#############################################################################

def get_object_item(val, key):
    try:
        return val.fields[key]
    except KeyError:
        raise bottle.HTTPError(
        code=404, output='object does not have field "%s".' % key
    )

def get_array_item(val, key):
    key = int(key)

    try:
        return val[key]
    except KeyError:
        raise bottle.HTTPError(
        code=404, output='array does not have index %s.' % key
    )

def get_item(val, key):
    if isinstance(val, andbug.Array):
        return get_array_item(val, key)
        
    if isinstance(val, andbug.Object):
        return get_object_item(val, key)
        
    raise bottle.HTTPError(
        code=404, output='cannot navigate type %s.' % type(val).__name__
    )

@bottle.route('/t/:tid/:fid/:key')
@bottle.route('/t/:tid/:fid/:key/:path#.*#')
def view_slot(tid, fid, key, path=None):
    'lists the values in the frame'
    tid, fid, key = int(tid), int(fid), str(key)
    threads = get_threads()
    value = tuple(threads[tid].frames)[fid].values.get(key)
    if path is not None:
        path = path.split('/')

    while path:
        key = path[0]
        path = path[1:]
        value = get_item(value, key)

    data = json.dumps(view(value))
    bottle.response.content_type = 'application/json'
    return data

###################################################### THE THREAD FOREST (TT)
# The thread-forest API produces a JSON summary of the threads and their
# frame stacks.  This is consolidated into one data structure to reduce
# round trip latency.
#############################################################################

#TODO: INSULATE
def seq_frame(frame, url):
    if not url.endswith('/'):
        url += '/'
    seq = [info(frame), frame.native]
    for key, val in frame.values.iteritems():
        seq.append((key, info(val), url + key))
    return seq

def seq_thread(thread, url):
    if not url.endswith('/'): 
        url += '/'
    seq = [info(thread)]
    frames = thread.frames
    for i in range(len(frames)):
        seq.append(seq_frame(frames[i], url + str(i)))
    return seq

def seq_process():            
    threads = get_threads()
    return list(
        seq_thread(threads[i], '/t/%s/' % i) for i in range(len(threads))
    )

@bottle.route('/tt')
def json_process():
    data = json.dumps(seq_process())
    bottle.response.content_type = 'application/json'
    return data

############################################################## FRONT SIDE (/)
# The front-side interface uses the JSON API with jQuery and jQuery UI to
# present a coherent 'one-page' interface to the user; embeds the process
# forest for efficiency.
#############################################################################

@bottle.route('/')
def frontend():
    return bottle.template('frontend', forest=json.dumps(seq_process()))

################################################################### BOOTSTRAP
# Bottle assumes that the server session will dominate the process, and does
# not handle being spun up and down on demand.  Navi does not depend heavily
# on Bottle, so this could be decoupled and put under WSGIREF.
#############################################################################

def navi_loop(p):
    # Look, bottle makes me do sad things..
    global proc
    proc = p
    
    bottle.debug(True)
    bottle.run(
        host='localhost',
        port=8080,
        reloader=False,
        quiet=True
    )

svr = None

@andbug.command.action('')
def navi(ctxt):
    'starts an http server for browsing process state'
    global svr
    if svr is not None:
        andbug.screed.section('navigation process already running')
        return

    with andbug.screed.section(
        'navigating process state at http://localhost:8080'
    ):
        andbug.screed.item('Process suspended for navigation.')
        ctxt.sess.suspend()


    svr = threading.Thread(target=lambda: navi_loop(ctxt.sess))
    svr.daemon = 1 if ctxt.shell else 0
    svr.start()
