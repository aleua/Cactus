import os
import sys
import shutil
import logging
import subprocess
import webbrowser
import getpass
import imp
import base64
import traceback
import socket
import tempfile
import tarfile

import boto

from .config import Config
from .utils import *
from .page import Page
from .static import Static
from .listener import Listener
from .file import File
from .server import Server, RequestHandler
from .browser import browserReload, browserReloadCSS


class Site(object):
	_path = None
	
	def __init__(self, path, optimize = False):
		self.path = path
		self.verify()
		self.config = Config(self.paths['config'])
		self.optimize = optimize

	@property
	def path(self):
		return self._path

	@path.setter
	def path(self, path):
		self._path = path

		self.paths = {
			'config': os.path.join(path, 'config.json'),
			'build': os.path.join(path, '.build'),
			'pages': os.path.join(path, 'pages'),
			'templates': os.path.join(path, 'templates'),
			'plugins': os.path.join(path, 'plugins'),
			'static': os.path.join(path, 'static'),
			'script': os.path.join(os.getcwd(), __file__)
		}
	
	def setup(self):
		"""
		Configure django to use both our template and pages folder as locations
		to look for included templates.
		"""
		try:
			from django.conf import settings
			settings.configure(
				TEMPLATE_DIRS=[self.paths['templates'], self.paths['pages']],
				INSTALLED_APPS=['django.contrib.markup']
			)
		except:
			pass
	
	def verify(self):
		"""
		Check if this path looks like a Cactus website
		"""
		while 1:
			for p in ['pages', 'static', 'templates', 'plugins']:
				if not os.path.isdir(os.path.join(self.path, p)):
					new_path = os.path.normpath(os.path.join(self.path, os.path.pardir))
					if new_path == self.path:
						break
					self.path = new_path
					continue
			return
		logging.info('This does not look like a (complete) cactus project (missing "%s" subfolder)', p)
		sys.exit()
	
	def bootstrap(self):
		"""
		Bootstrap a new project at a given path.
		"""
		
		from .skeleton import data
		
		skeletonFile = tempfile.NamedTemporaryFile(delete=False, suffix='.tar.gz')
		skeletonFile.write(base64.b64decode(data))
		skeletonFile.close()

		os.mkdir(self.path)
		
		skeletonArchive = tarfile.open(name=skeletonFile.name, mode='r')
		skeletonArchive.extractall(path=self.path)
		skeletonArchive.close()
		
		logging.info('New project generated at %s', self.path)

	def context(self):
		"""
		Base context for the site: all the html pages.
		"""
		return {'CACTUS': {'pages': [p for p in self.pages() if p.path.endswith('.html')]}}
	
	def clean(self):
		"""
		Remove all build files.
		"""
		if os.path.isdir(self.paths['build']):
			shutil.rmtree(self.paths['build'])
	
	def build(self):
		"""
		Generate fresh site from templates.
		"""

		# Set up django settings
		self.setup()

		# Bust the context cache
		self._contextCache = self.context()
		
		# Load the plugin code, because we want fresh plugin code on build
		# refreshes if we're running the web server with listen.
		self.loadPlugins()
		
		logging.info('Plugins: %s', ', '.join([p.id for p in self._plugins]))

		self.pluginMethod('preBuild', self)
		
		# Make sure the build path exists
		if not os.path.exists(self.paths['build']):
			os.mkdir(self.paths['build'])
		
		# Copy the static files
		self.buildStatic()
		
		# Render the pages to their output files
		
		# Uncomment for non threaded building, crashes randomly
		multiMap = map
		
		multiMap(lambda p: p.build(), self.pages())
		
		self.pluginMethod('postBuild', self)
	
	@memoize
	def static(self):
		paths = fileList(self.paths['static'], relative = True)
		return [Static(self, path) for path in paths]

	def get_path_for_static(self, src_path):
		if is_external(src_path):
			return src_path

		static_dict = {static.src_path: static for static in self.static()}

		try:
			if src_path[0] == '/': # Handle len < 2..
				return '/' + static_dict[src_path[1:]].build_path
			else:
				return static_dict[src_path].build_path
		except KeyError:
			raise Exception('Static does not exist: {0}'.format(src_path))


	def buildStatic(self):
		"""
		Move static files to build folder. To be fast we symlink it for now,
		but we should actually copy these files in the future.
		"""
		multiMap = map
		
		multiMap(lambda s: s.build(), self.static())


	@memoize
	def pages(self):
		"""
		List of pages.
		"""
		paths = fileList(self.paths['pages'], relative=True)
		paths = filter(lambda x: not x.endswith("~"), paths)
		return [Page(self, p) for p in paths]

	def serve(self, browser=True, port=8000):
		"""
		Start a http server and rebuild on changes.
		"""
		self.clean()
		self.build()
	
		logging.info('Running webserver at 0.0.0.0:%s for %s' % (port, self.paths['build']))
		logging.info('Type control-c to exit')
	
		os.chdir(self.paths['build'])
		
		def rebuild(changes):
			logging.info('*** Rebuilding (%s changed)' % self.path)
			
			# We will pause the listener while building so scripts that alter the output
			# like coffeescript and less don't trigger the listener again immediately.
			self.listener.pause()
			try: self.build()
			except Exception, e: 
				logging.info('*** Error while building\n%s', e)
				traceback.print_exc(file=sys.stdout)
			
			# When we have changes, we want to refresh the browser tabs with the updates.
			# Mostly we just refresh the browser except when there are just css changes,
			# then we reload the css in place.
			if  len(changes["added"]) == 0 and \
				len(changes["deleted"]) == 0 and \
				set(map(lambda x: os.path.splitext(x)[1], changes["changed"])) == set([".css"]):
				browserReloadCSS('http://127.0.0.1:%s' % port)
			else:
				browserReload('http://127.0.0.1:%s' % port)
			
			self.listener.resume()
	
		self.listener = Listener(self.path, rebuild, ignore=lambda x: '/.build/' in x)
		self.listener.run()
		
		try:
			httpd = Server(("", port), RequestHandler)
		except socket.error, e:
			logging.info('Could not start webserver, port is in use. To use another port:')
			logging.info('  cactus serve %s' % (int(port) + 1))
			return
		
		if browser is True:
			webbrowser.open('http://127.0.0.1:%s' % port)

		try: 
			httpd.serve_forever() 
		except (KeyboardInterrupt, SystemExit):
			httpd.server_close() 

		logging.info('See you!')

	
	def upload(self):
		"""
		Upload the site to the server.
		"""

		# Make sure we have internet
		if not internetWorking():
			logging.info('There does not seem to be internet here, check your connection')
			return

		logging.debug('Start upload')
		
		self.clean()
		self.build()
		
		logging.debug('Start preDeploy')
		self.pluginMethod('preDeploy', self)
		logging.debug('End preDeploy')
		
		# Get access information from the config or the user
		awsAccessKey = self.config.get('aws-access-key') or \
			raw_input('Amazon access key (http://bit.ly/Agl7A9): ').strip()
		awsSecretKey = getpassword('aws', awsAccessKey) or \
			getpass._raw_input('Amazon secret access key (will be saved in keychain): ').strip()
		
		# Try to fetch the buckets with the given credentials
		connection = boto.connect_s3(awsAccessKey.strip(), awsSecretKey.strip())
		
		logging.debug('Start get_all_buckets')
		# Exit if the information was not correct
		try:
			buckets = connection.get_all_buckets()
		except:
			logging.info('Invalid login credentials, please try again...')
			return
		logging.debug('end get_all_buckets')
		
		# If it was correct, save it for the future
		self.config.set('aws-access-key', awsAccessKey)
		self.config.write()
	
		setpassword('aws', awsAccessKey, awsSecretKey)
	
		awsBucketName = self.config.get('aws-bucket-name') or \
			raw_input('S3 bucket name (www.yoursite.com): ').strip().lower()
	
		if awsBucketName not in [b.name for b in buckets]:
			if raw_input('Bucket does not exist, create it? (y/n): ') == 'y':
				
				logging.debug('Start create_bucket')
				try:
					awsBucket = connection.create_bucket(awsBucketName, policy='public-read')
				except boto.exception.S3CreateError, e:
					logging.info('Bucket with name %s already is used by someone else, please try again with another name' % awsBucketName)
					return
				logging.debug('end create_bucket')
				
				# Configure S3 to use the index.html and error.html files for indexes and 404/500s.
				awsBucket.configure_website('index.html', 'error.html')

				self.config.set('aws-bucket-website', awsBucket.get_website_endpoint())
				self.config.set('aws-bucket-name', awsBucketName)
				self.config.write()

				logging.info('Bucket %s was selected with website endpoint %s' % (self.config.get('aws-bucket-name'), self.config.get('aws-bucket-website')))
				logging.info('You can learn more about s3 (like pointing to your own domain) here: https://github.com/koenbok/Cactus')


			else: return
		else:
			
			# Grab a reference to the existing bucket
			for b in buckets:
				if b.name == awsBucketName:
					awsBucket = b

		self.config.set('aws-bucket-website', awsBucket.get_website_endpoint())
		self.config.set('aws-bucket-name', awsBucketName)
		self.config.write()
		
		logging.info('Uploading site to bucket %s' % awsBucketName)
		
		# Upload all files concurrently in a thread pool
		totalFiles = multiMap(lambda p: p.upload(awsBucket), self.files())
		changedFiles = [r for r in totalFiles if r['changed'] == True]
		
		self.pluginMethod('postDeploy', self)
		
		# Display done message and some statistics
		logging.info('\nDone\n')
		
		logging.info('%s total files with a size of %s' % \
			(len(totalFiles), fileSize(sum([r['size'] for r in totalFiles]))))
		logging.info('%s changed files with a size of %s' % \
			(len(changedFiles), fileSize(sum([r['size'] for r in changedFiles]))))
		
		logging.info('\nhttp://%s\n' % self.config.get('aws-bucket-website'))


	def files(self):
		"""
		List of build files.
		"""
		return [File(self, p) for p in fileList(self.paths['build'], relative=True)]


	def loadPlugins(self, force=False):
		"""
		Load plugins from the plugins directory and import the code.
		"""
		
		plugins = []
		
		# Figure out the files that can possibly be plugins
		for pluginPath in fileList(self.paths['plugins']):
	
			if not pluginPath.endswith('.py'):
				continue

			if 'disabled' in pluginPath:
				continue
			
			pluginHandle = os.path.splitext(os.path.basename(pluginPath))[0]
			
			# Try to load the code from a plugin
			try:
				plugin = imp.load_source('plugin_%s' % pluginHandle, pluginPath)
			except Exception, e:
				logging.info('Error: Could not load plugin at path %s\n%s' % (pluginPath, e))
				sys.exit()
			
			# Set an id based on the file name
			plugin.id = pluginHandle
			
			plugins.append(plugin)
		
		# Sort the plugins by their defined order (optional)
		def getOrder(plugin):
			if hasattr(plugin, 'ORDER'):
				return plugin.ORDER
			return -1
		
		self._plugins = sorted(plugins, key=getOrder)
	
	def pluginMethod(self, method, *args, **kwargs):
		"""
		Run this method on all plugins
		"""
		
		if not hasattr(self, '_plugins'):
			self.loadPlugins()
		
		for plugin in self._plugins:
			if hasattr(plugin, method):
				getattr(plugin, method)(*args, **kwargs)
