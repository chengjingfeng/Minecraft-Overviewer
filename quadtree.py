#    This file is part of the Minecraft Overviewer.
#
#    Minecraft Overviewer is free software: you can redistribute it and/or
#    modify it under the terms of the GNU General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or (at
#    your option) any later version.
#
#    Minecraft Overviewer is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
#    Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with the Overviewer.  If not, see <http://www.gnu.org/licenses/>.

import multiprocessing
import itertools
import os
import os.path
import functools
import re
import shutil
import collections
import json
import logging
import util
import cPickle
import stat
import errno 
from time import gmtime, strftime, sleep

from PIL import Image

import nbt
import chunk
from optimizeimages import optimize_image
import composite


"""
This module has routines related to generating a quadtree of tiles

"""

def mirror_dir(src, dst, entities=None):
    '''copies all of the entities from src to dst'''
    if not os.path.exists(dst):
        os.mkdir(dst)
    if entities and type(entities) != list: raise Exception("Expected a list, got a %r instead" % type(entities))

    for entry in os.listdir(src):
        if entities and entry not in entities: continue
        if os.path.isdir(os.path.join(src,entry)):
            mirror_dir(os.path.join(src, entry), os.path.join(dst, entry))
        elif os.path.isfile(os.path.join(src,entry)):
            try:
                shutil.copy(os.path.join(src, entry), os.path.join(dst, entry))
            except IOError:
                # maybe permission problems?
                os.chmod(os.path.join(src, entry), stat.S_IRUSR)
                os.chmod(os.path.join(dst, entry), stat.S_IWUSR)
                shutil.copy(os.path.join(src, entry), os.path.join(dst, entry))
                # if this stills throws an error, let it propagate up

def iterate_base4(d):
    """Iterates over a base 4 number with d digits"""
    return itertools.product(xrange(4), repeat=d)

def catch_keyboardinterrupt(func):
    """Decorator that catches a keyboardinterrupt and raises a real exception
    so that multiprocessing will propagate it properly"""
    @functools.wraps(func)
    def newfunc(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except KeyboardInterrupt:
            logging.error("Ctrl-C caught!")
            raise Exception("Exiting")
        except:
            import traceback
            traceback.print_exc()
            raise
    return newfunc

class QuadtreeGen(object):
    def __init__(self, worldobj, destdir, depth=None, tiledir="tiles", imgformat=None, optimizeimg=None, lighting=False, night=False, spawn=False):
        """Generates a quadtree from the world given into the
        given dest directory

        worldobj is a world.WorldRenderer object that has already been processed

        If depth is given, it overrides the calculated value. Otherwise, the
        minimum depth that contains all chunks is calculated and used.

        """
        assert(imgformat)
        self.imgformat = imgformat
        self.optimizeimg = optimizeimg
        
        self.lighting = lighting
        self.night = night
        self.spawn = spawn

        # Make the destination dir
        if not os.path.exists(destdir):
            os.mkdir(destdir)
        self.tiledir = tiledir

        if depth is None:
            # Determine quadtree depth (midpoint is always 0,0)
            for p in xrange(15):
                # Will 2^p tiles wide and high suffice?

                # X has twice as many chunks as tiles, then halved since this is a
                # radius
                xradius = 2**p
                # Y has 4 times as many chunks as tiles, then halved since this is
                # a radius
                yradius = 2*2**p
                if xradius >= worldobj.maxcol and -xradius <= worldobj.mincol and \
                        yradius >= worldobj.maxrow and -yradius <= worldobj.minrow:
                    break
            else:
                raise ValueError("Your map is waaaay too big! Use the '-z' or '--zoom' options.")

            self.p = p
        else:
            self.p = depth
            xradius = 2**depth
            yradius = 2*2**depth

        # Make new row and column ranges
        self.mincol = -xradius
        self.maxcol = xradius
        self.minrow = -yradius
        self.maxrow = yradius

        self.world = worldobj
        self.destdir = destdir

    def print_statusline(self, complete, total, level, unconditional=False):
        if unconditional:
            pass
        elif complete < 100:
            if not complete % 25 == 0:
                return
        elif complete < 1000:
            if not complete % 100 == 0:
                return
        else:
            if not complete % 1000 == 0:
                return
        logging.info("{0}/{1} tiles complete on level {2}/{3}".format(
                complete, total, level, self.p))

    def write_html(self, skipjs=False):
        """Writes out config.js, marker.js, and region.js
        Copies web assets into the destdir"""
        zoomlevel = self.p
        imgformat = self.imgformat
        configpath = os.path.join(util.get_program_path(), "config.js")

        config = open(configpath, 'r').read()
        config = config.replace(
                "{maxzoom}", str(zoomlevel))
        config = config.replace(
                "{imgformat}", str(imgformat))
                
        with open(os.path.join(self.destdir, "config.js"), 'w') as output:
            output.write(config)

        # Write a blank image
        blank = Image.new("RGBA", (1,1))
        tileDir = os.path.join(self.destdir, self.tiledir)
        if not os.path.exists(tileDir): os.mkdir(tileDir)
        blank.save(os.path.join(tileDir, "blank."+self.imgformat))

        # copy web assets into destdir:
        mirror_dir(os.path.join(util.get_program_path(), "web_assets"), self.destdir)

        # Add time in index.html
        indexpath = os.path.join(self.destdir, "index.html")

        index = open(indexpath, 'r').read()
        index = index.replace(
                "{time}", str(strftime("%a, %d %b %Y %H:%M:%S +0000", gmtime())))

        with open(os.path.join(self.destdir, "index.html"), 'w') as output:
            output.write(index)

        if skipjs:
            return

        # since we will only discover PointsOfInterest in chunks that need to be 
        # [re]rendered, POIs like signs in unchanged chunks will not be listed
        # in self.world.POI.  To make sure we don't remove these from markers.js
        # we need to merge self.world.POI with the persistant data in world.PersistentData

        self.world.POI += filter(lambda x: x['type'] != 'spawn', self.world.persistentData['POI'])

        # write out the default marker table
        with open(os.path.join(self.destdir, "markers.js"), 'w') as output:
            output.write("var markerData=%s" % json.dumps(self.world.POI))
        
        # save persistent data
        self.world.persistentData['POI'] = self.world.POI
        with open(self.world.pickleFile,"wb") as f:
            cPickle.dump(self.world.persistentData,f)

        # write out the default (empty, but documented) region table
        with open(os.path.join(self.destdir, "regions.js"), 'w') as output:
            output.write('var regionData=[\n')
            output.write('  // {"color": "#FFAA00", "opacity": 0.5, "closed": true, "path": [\n')
            output.write('  //   {"x": 0, "y": 0, "z": 0},\n')
            output.write('  //   {"x": 0, "y": 10, "z": 0},\n')
            output.write('  //   {"x": 0, "y": 0, "z": 10}\n')
            output.write('  // ]},\n')
            output.write('];')
        
    def _get_cur_depth(self):
        """How deep is the quadtree currently in the destdir? This glances in
        config.js to see what maxZoom is set to.
        returns -1 if it couldn't be detected, file not found, or nothing in
        config.js matched
        """
        indexfile = os.path.join(self.destdir, "config.js")
        if not os.path.exists(indexfile):
            return -1
        matcher = re.compile(r"maxZoom:\s*(\d+)")
        p = -1
        for line in open(indexfile, "r"):
            res = matcher.search(line)
            if res:
                p = int(res.group(1))
                break
        return p

    def _increase_depth(self):
        """Moves existing tiles into place for a larger tree"""
        getpath = functools.partial(os.path.join, self.destdir, self.tiledir)

        # At top level of the tree:
        # quadrant 0 is now 0/3
        # 1 is now 1/2
        # 2 is now 2/1
        # 3 is now 3/0
        # then all that needs to be done is to regenerate the new top level
        for dirnum in range(4):
            newnum = (3,2,1,0)[dirnum]

            newdir = "new" + str(dirnum)
            newdirpath = getpath(newdir)

            files = [str(dirnum)+"."+self.imgformat, str(dirnum)]
            newfiles = [str(newnum)+"."+self.imgformat, str(newnum)]

            os.mkdir(newdirpath)
            for f, newf in zip(files, newfiles):
                p = getpath(f)
                if os.path.exists(p):
                    os.rename(p, getpath(newdir, newf))
            os.rename(newdirpath, getpath(str(dirnum)))

    def _decrease_depth(self):
        """If the map size decreases, or perhaps the user has a depth override
        in effect, re-arrange existing tiles for a smaller tree"""
        getpath = functools.partial(os.path.join, self.destdir, self.tiledir)

        # quadrant 0/3 goes to 0
        # 1/2 goes to 1
        # 2/1 goes to 2
        # 3/0 goes to 3
        # Just worry about the directories here, the files at the top two
        # levels are cheap enough to replace
        if os.path.exists(getpath("0", "3")):
            os.rename(getpath("0", "3"), getpath("new0"))
            shutil.rmtree(getpath("0"))
            os.rename(getpath("new0"), getpath("0"))

        if os.path.exists(getpath("1", "2")):
            os.rename(getpath("1", "2"), getpath("new1"))
            shutil.rmtree(getpath("1"))
            os.rename(getpath("new1"), getpath("1"))

        if os.path.exists(getpath("2", "1")):
            os.rename(getpath("2", "1"), getpath("new2"))
            shutil.rmtree(getpath("2"))
            os.rename(getpath("new2"), getpath("2"))

        if os.path.exists(getpath("3", "0")):
            os.rename(getpath("3", "0"), getpath("new3"))
            shutil.rmtree(getpath("3"))
            os.rename(getpath("new3"), getpath("3"))

    def _apply_render_worldtiles(self, pool,batch_size):
        """Returns an iterator over result objects. Each time a new result is
        requested, a new task is added to the pool and a result returned.
        """
             
        batch = []
        tiles = 0
        for path in iterate_base4(self.p):
            # Get the range for this tile
            colstart, rowstart = self._get_range_by_path(path)
            colend = colstart + 2
            rowend = rowstart + 4

            # This image is rendered at:
            dest = os.path.join(self.destdir, self.tiledir, *(str(x) for x in path))
            #logging.debug("this is rendered at %s", dest)

            # And uses these chunks
            tilechunks = self._get_chunks_in_range(colstart, colend, rowstart,
                    rowend)
            #logging.debug(" tilechunks: %r", tilechunks)
            
            # Put this in the batch to be submited to the pool
            # (even if tilechunks is empty, render_worldtile will delete
            # existing images if appropriate)                           
            batch.append((tilechunks, colstart, colend, rowstart, rowend, dest))
            tiles += 1
            if tiles >= batch_size:
                tiles = 0
                yield pool.apply_async(func=render_worldtile_batch, args= (self,batch))
                batch = []

        if tiles > 0:
            yield pool.apply_async(func=render_worldtile_batch, args= (self,batch))


    def _apply_render_inntertile(self, pool, zoom,batch_size):
        """Same as _apply_render_worltiles but for the inntertile routine.
        Returns an iterator that yields result objects from tasks that have
        been applied to the pool.
        """
        batch = []
        tiles = 0        
        for path in iterate_base4(zoom):
            # This image is rendered at:
            dest = os.path.join(self.destdir, self.tiledir, *(str(x) for x in path[:-1]))
            name = str(path[-1])
            
            batch.append((dest, name, self.imgformat, self.optimizeimg))
            tiles += 1
            if tiles >= batch_size:
                tiles = 0
                yield pool.apply_async(func=render_innertile_batch, args= (self,batch))
                batch = []
            
        if tiles > 0:            
            yield pool.apply_async(func=render_innertile_batch, args= (self,batch))

    def go(self, procs):
        """Renders all tiles"""

        curdepth = self._get_cur_depth()
        if curdepth != -1:
            if self.p > curdepth:
                logging.warning("Your map seemes to have expanded beyond its previous bounds.")
                logging.warning( "Doing some tile re-arrangements... just a sec...")
                for _ in xrange(self.p-curdepth):
                    self._increase_depth()
            elif self.p < curdepth:
                logging.warning("Your map seems to have shrunk. Re-arranging tiles, just a sec...")
                for _ in xrange(curdepth - self.p):
                    self._decrease_depth()

        # Create a pool
        if procs == 1:
            pool = FakePool()
        else:
            pool = multiprocessing.Pool(processes=procs)

        # Render the highest level of tiles from the chunks
        results = collections.deque()
        complete = 0
        total = 4**self.p
        logging.info("Rendering highest zoom level of tiles now.")
        logging.info("There are {0} tiles to render".format(total))
        logging.info("There are {0} total levels to render".format(self.p))
        logging.info("Don't worry, each level has only 25% as many tiles as the last.")
        logging.info("The others will go faster")
        count = 0
        batch_size = 50
        for result in self._apply_render_worldtiles(pool,batch_size):
            results.append(result)
            if len(results) > (10000/batch_size):
                # Empty the queue before adding any more, so that memory
                # required has an upper bound
                while len(results) > (500/batch_size):
                    complete += results.popleft().get()
                    self.print_statusline(complete, total, 1)

        # Wait for the rest of the results
        while len(results) > 0:

            complete += results.popleft().get()
            self.print_statusline(complete, total, 1)

        self.print_statusline(complete, total, 1, True)

        # Now do the other layers
        for zoom in xrange(self.p-1, 0, -1):
            level = self.p - zoom + 1
            assert len(results) == 0
            complete = 0
            total = 4**zoom
            logging.info("Starting level {0}".format(level))
            for result in self._apply_render_inntertile(pool, zoom,batch_size):
                results.append(result)
                if len(results) > (10000/batch_size):
                    while len(results) > (500/batch_size):
                        complete += results.popleft().get()
                        self.print_statusline(complete, total, level)
            # Empty the queue
            while len(results) > 0:
                complete += results.popleft().get()
                self.print_statusline(complete, total, level)

            self.print_statusline(complete, total, level, True)

            logging.info("Done")

        pool.close()
        pool.join()

        # Do the final one right here:
        render_innertile(os.path.join(self.destdir, self.tiledir), "base", self.imgformat, self.optimizeimg)

    def _get_range_by_path(self, path):
        """Returns the x, y chunk coordinates of this tile"""
        x, y = self.mincol, self.minrow
        
        xsize = self.maxcol
        ysize = self.maxrow

        for p in path:
            if p in (1, 3):
                x += xsize
            if p in (2, 3):
                y += ysize
            xsize //= 2
            ysize //= 2

        return x, y

    def _get_chunks_in_range(self, colstart, colend, rowstart, rowend):
        """Get chunks that are relevant to the tile rendering function that's
        rendering that range"""
        chunklist = []
        unconvert_coords = self.world.unconvert_coords
        #get_region_path = self.world.get_region_path
        get_region = self.world.regionfiles.get
        for row in xrange(rowstart-16, rowend+1):
            for col in xrange(colstart, colend+1):
                # due to how chunks are arranged, we can only allow
                # even row, even column or odd row, odd column
                # otherwise, you end up with duplicates!
                if row % 2 != col % 2:
                    continue
                
                # return (col, row, chunkx, chunky, regionpath)
                chunkx, chunky = unconvert_coords(col, row)
                #c = get_region_path(chunkx, chunky)
                _, _, c = get_region((chunkx//32, chunky//32),(None,None,None));
                if c is not None:
                    chunklist.append((col, row, chunkx, chunky, c))
        return chunklist

@catch_keyboardinterrupt
def render_innertile_batch(quadtree, batch):    
    count = 0
    #logging.debug("{0} working on batch of size {1}".format(os.getpid(),len(batch)))
    for job in batch:
        count += 1
        render_innertile(job[0],job[1],job[2],job[3])
    return count
    
def render_innertile(dest, name, imgformat, optimizeimg):
    """
    Renders a tile at os.path.join(dest, name)+".ext" by taking tiles from
    os.path.join(dest, name, "{0,1,2,3}.png")
    """
    imgpath = os.path.join(dest, name) + "." + imgformat

    if name == "base":
        quadPath = [[(0,0),os.path.join(dest, "0." + imgformat)],[(192,0),os.path.join(dest, "1." + imgformat)], [(0, 192),os.path.join(dest, "2." + imgformat)],[(192,192),os.path.join(dest, "3." + imgformat)]]
    else:
        quadPath = [[(0,0),os.path.join(dest, name, "0." + imgformat)],[(192,0),os.path.join(dest, name, "1." + imgformat)],[(0, 192),os.path.join(dest, name, "2." + imgformat)],[(192,192),os.path.join(dest, name, "3." + imgformat)]]    
   
    #stat the tile, we need to know if it exists or it's mtime
    try:    
        tile_mtime =  os.stat(imgpath)[stat.ST_MTIME];
    except OSError, e:
        if e.errno != errno.ENOENT:
            raise
        tile_mtime = None
        
    #check mtimes on each part of the quad, this also checks if they exist
    needs_rerender = tile_mtime is None
    quadPath_filtered = []
    for path in quadPath:
        try:
            quad_mtime = os.stat(path[1])[stat.ST_MTIME]; 
            quadPath_filtered.append(path)
            if quad_mtime > tile_mtime:     
                needs_rerender = True            
        except OSError:
            # We need to stat all the quad files, so keep looping
            pass      
    # do they all not exist?
    if quadPath_filtered == []:
        if tile_mtime is not None:
            os.unlink(imgpath)
        return
    # quit now if we don't need rerender            
    if not needs_rerender:
        return    
    #logging.debug("writing out innertile {0}".format(imgpath))

    # Create the actual image now
    img = Image.new("RGBA", (384, 384), (38,92,255,0))
    
    # we'll use paste (NOT alpha_over) for quadtree generation because
    # this is just straight image stitching, not alpha blending
    
    for path in quadPath_filtered:
        try:
            quad = Image.open(path[1]).resize((192,192), Image.ANTIALIAS)
            img.paste(quad, path[0])
        except Exception, e:
            logging.warning("Couldn't open %s. It may be corrupt, you may need to delete it. %s", path[1], e)

    # Save it
    if imgformat == 'jpg':
        img.save(imgpath, quality=95, subsampling=0)
    else: # png
        img.save(imgpath)
        if optimizeimg:
            optimize_image(imgpath, imgformat, optimizeimg)

@catch_keyboardinterrupt
def render_worldtile_batch(quadtree, batch):    
    count = 0
    #logging.debug("{0} working on batch of size {1}".format(os.getpid(),len(batch)))
    for job in batch:
        count += 1
        render_worldtile(quadtree,job[0],job[1],job[2],job[3],job[4],job[5])
    return count

def render_worldtile(quadtree, chunks, colstart, colend, rowstart, rowend, path):
    """Renders just the specified chunks into a tile and save it. Unlike usual
    python conventions, rowend and colend are inclusive. Additionally, the
    chunks around the edges are half-way cut off (so that neighboring tiles
    will render the other half)

    chunks is a list of (col, row, chunkx, chunky, filename) of chunk
    images that are relevant to this call (with their associated regions)

    The image is saved to path+"."+quadtree.imgformat

    If there are no chunks, this tile is not saved (if it already exists, it is
    deleted)

    Standard tile size has colend-colstart=2 and rowend-rowstart=4

    There is no return value
    """    
    
    # width of one chunk is 384. Each column is half a chunk wide. The total
    # width is (384 + 192*(numcols-1)) since the first column contributes full
    # width, and each additional one contributes half since they're staggered.
    # However, since we want to cut off half a chunk at each end (384 less
    # pixels) and since (colend - colstart + 1) is the number of columns
    # inclusive, the equation simplifies to:
    width = 192 * (colend - colstart)
    # Same deal with height
    height = 96 * (rowend - rowstart)

    # The standard tile size is 3 columns by 5 rows, which works out to 384x384
    # pixels for 8 total chunks. (Since the chunks are staggered but the grid
    # is not, some grid coordinates do not address chunks) The two chunks on
    # the middle column are shown in full, the two chunks in the middle row are
    # half cut off, and the four remaining chunks are one quarter shown.
    # The above example with cols 0-3 and rows 0-4 has the chunks arranged like this:
    #   0,0         2,0
    #         1,1
    #   0,2         2,2
    #         1,3
    #   0,4         2,4

    # Due to how the tiles fit together, we may need to render chunks way above
    # this (since very few chunks actually touch the top of the sky, some tiles
    # way above this one are possibly visible in this tile). Render them
    # anyways just in case). "chunks" should include up to rowstart-16

    imgpath = path + "." + quadtree.imgformat
    
    world = quadtree.world
    # first, remove chunks from `chunks` that don't actually exist in
    # their region files
    def chunk_exists(chunk):
        _, _, chunkx, chunky, region = chunk
        r = world.load_region(region)
        return r.chunkExists(chunkx, chunky)            
    chunks = filter(chunk_exists, chunks)

    #stat the file, we need to know if it exists or it's mtime
    try:    
        tile_mtime =  os.stat(imgpath)[stat.ST_MTIME];
    except OSError, e:
        if e.errno != errno.ENOENT:
            raise
        tile_mtime = None
        
    if not chunks:
        # No chunks were found in this tile
        if tile_mtime is not None:
            os.unlink(imgpath)
        return None

    # Create the directory if not exists
    dirdest = os.path.dirname(path)
    if not os.path.exists(dirdest):
        try:
            os.makedirs(dirdest)
        except OSError, e:
            # Ignore errno EEXIST: file exists. Since this is multithreaded,
            # two processes could conceivably try and create the same directory
            # at the same time.            
            if e.errno != errno.EEXIST:
                raise
    
    # check chunk mtimes to see if they are newer
    try:
        #tile_mtime = os.path.getmtime(imgpath)
        regionMtimes = {}
        needs_rerender = False
        for col, row, chunkx, chunky, regionfile in chunks:
            # check region file mtime first. 
            # Note: we cache the value since it's actually very likely we will have multipule chunks in the same region, and syscalls are expensive
            regionMtime = regionMtimes.get(regionfile,None)
            if  regionMtime is None:
                regionMtime = os.path.getmtime(regionfile)  
                regionMtimes[regionfile] = regionMtime 
            if regionMtime <= tile_mtime:
                continue
            
            # checking chunk mtime
            region = world.load_region(regionfile)
            if region.get_chunk_timestamp(chunkx, chunky) > tile_mtime:
                needs_rerender = True
                break
        
        # if after all that, we don't need a rerender, return
        if not needs_rerender:
            return None
    except OSError:
        # couldn't get tile mtime, skip check
        pass
    
    #logging.debug("writing out worldtile {0}".format(imgpath))

    # Compile this image
    tileimg = Image.new("RGBA", (width, height), (38,92,255,0))

    # col colstart will get drawn on the image starting at x coordinates -(384/2)
    # row rowstart will get drawn on the image starting at y coordinates -(192/2)
    for col, row, chunkx, chunky, regionfile in chunks:
        xpos = -192 + (col-colstart)*192
        ypos = -96 + (row-rowstart)*96

        # draw the chunk!
        # TODO POI queue
        chunk.render_to_image((chunkx, chunky), tileimg, (xpos, ypos), quadtree, False, None)

    # Save them
    tileimg.save(imgpath)

    if quadtree.optimizeimg:
        optimize_image(imgpath, quadtree.imgformat, quadtree.optimizeimg)

class FakeResult(object):
    def __init__(self, res):
        self.res = res
    def get(self):
        return self.res
class FakePool(object):
    """A fake pool used to render things in sync. Implements a subset of
    multiprocessing.Pool"""
    def apply_async(self, func, args=(), kwargs=None):
        if not kwargs:
            kwargs = {}
        result = func(*args, **kwargs)
        return FakeResult(result)
    def close(self):
        pass
    def join(self):
        pass
