#!/usr/bin/env python

import re
import os
import logging
from datetime import datetime

from google.appengine.api import memcache
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.api import users
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.api import mail

from django.utils import simplejson

from lib import BaseRequest, get_cache, slugify
import settings
from models import Project, Issue, DatastoreFile
from ext.PyRSS2Gen import RSS2, RSSItem

webapp.template.register_template_library('tags.filters')

# regex for the patter to look for in the webhooks
GITBUG = re.compile('#gitbug[0-9]+')

# validate url
URL_RE = re.compile(
    r'^https?://' # http:// or https://
    r'(?:(?:[A-Z0-9-]+\.)+[A-Z]{2,6}|' #domain...
    r'localhost|' #localhost...
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})' # ...or ip
    r'(?::\d+)?' # optional port
    r'(?:/?|/\S+)$', re.IGNORECASE)


class Index(BaseRequest):
    "Home page. Shows either introductory info or a list of the users projects"
    def get(self):
        if users.get_current_user():
            # if we have a user then get their projects
            projects = Project.all().filter('user =', users.get_current_user()).order('-created_date').fetch(50)
            context = {
                'projects': projects,
            }
            output = self.render("index.html", context)
        else:
            # otherwise it's a static page so cache for a while
            output = get_cache("home")
            if output is None:
                output = self.render("home.html")
                memcache.add("home", output, 3600)
        self.response.out.write(output)

class ProjectHandler(BaseRequest):
    "Individual project details and issue adding"
    def get(self, slug):
        # we want canonocal urls so redirect to add a trailing slash if needed
        if self.request.path[-1] != "/":
            self.redirect("%s/" % self.request.path, True)
            return
        
        user = users.get_current_user()
        output = None
        
        # if not logged in then use a cached version
        if not user:
            output = get_cache("project_%s" % slug)
        
        # if we don't have a cached version or are logged in
        if output is None:
            try:
                project = Project.all().filter('slug =', slug).fetch(1)[0]        
                issues = Issue.all().filter('project =', project)
                files = DatastoreFile.all().filter('project =', project)
            except IndexError:
                self.render_404()
                return 
                
            logging.info("Files in this project: %d" % files.count())
            # check to see if we have admin rights over this project
            if project.user == user or users.is_current_user_admin():
                owner = True
            else:
                owner = False            
            
            context = {
                'project': project,
                'issues': issues,
                'owner': owner,
                'files': files,
            }
            output = self.render("project.html", context)
        if not user:
            # only save a cached version if we're not logged in
            # so as to avoid revelaving user details
            memcache.add("project_%s" % slug, output, 3600)
        self.response.out.write(output)
        
    def post(self, slug):
        "Create an issue against this project"
        project = Project.all().filter('slug =', slug).fetch(1)[0]
        # get details from the form
        name = self.request.get("name")
        description = self.request.get("description")
        email = self.request.get("email")
        priority = self.request.get("priority")
        
        try:
            if Issue.all().filter('name =', name).filter('project =', project).count() == 0:
                issue = Issue(
                    name=name,
                    description=description,
                    project=project,
                    priority=priority,
                )
                if email:
                    issue.email = email
                issue.put()
                mail.send_mail(sender="prokontrol@gmail.com",
                    to=project.user.email(),
                    subject="[GitBug] New bug added to %s" % project.name,
                    body="""You requested to be emailed when a bug on GitBug was added:

Issue name: %s
Description: %s

Thanks for using GitBug <http://gitbug.appspot.com>. A very simple issue tracker.
    """ % (issue.name, issue.description))
                logging.info("issue created: %s in %s" % (name, project.name))
        except Exception, e:
            logging.error("error adding issue: %s" % e)
        
        self.redirect("/projects/%s/" % slug)

class ProjectJsonHandler(BaseRequest):
    "Project information in JSON"
    def get(self, slug):
        output = get_cache("project_%s_json" % slug)
        if output is None:
            project = Project.all().filter('slug =', slug).fetch(1)[0]        
            issues = Issue.all().filter('project =', project).order('fixed').order('created_date')

            issues_data = {}
            for issue in issues:
                # friendlier display of information
                if issue.fixed: 
                    status = "Fixed"
                else:
                    status = "Open"

                # set structure of inner json
                data = {
                    'internal_url': "%s/projects%s" % (settings.SYSTEM_URL, issue.internal_url),
                    'created_date': str(project.created_date)[0:19],
                    'description': issue.html,
                    'status': status,
                    'identifier': "#gitbug%s" % issue.identifier,
                }
                if issue.fixed and issue.fixed_description:
                    data['fixed_description'] = issue.fixed_description
                issues_data[issue.name] = data

            # set structure of outer json
            json = {
                'date': str(datetime.now())[0:19],
                'name': project.name,
                'internal_url': "%s/projects/%s/" % (settings.SYSTEM_URL, project.slug),
                'created_date': str(project.created_date)[0:19],
                'issues': issues_data,
            }
            
            if project.url:
                json['external_url'] = project.url

            # create the json
            output = simplejson.dumps(json)
            # cache it
            memcache.add("project_%s_json" % slug, output, 3600)
        # send the correct headers
        self.response.headers["Content-Type"] = "application/javascript; charset=utf8"
        self.response.out.write(output)
        
class ProjectRssHandler(BaseRequest):
    "Project as RSS, specifically lists issues"
    def get(self, slug):

        output = get_cache("project_%s_rss" % slug)
        if output is None:

            project = Project.all().filter('slug =', slug).fetch(1)[0]        
               
            # allow query string arguments to specify filters
            if self.request.get("open"):
                status_filter = True
                fixed = False
            elif self.request.get("closed"):
                status_filter = True
                fixed = True
            else:
                status_filter = None

            # if we have a filter then filter the results set
            if status_filter:
                issues = Issue.all().filter('project =', project).filter('fixed =', fixed).order('fixed').order('created_date')
            else:
                issues = Issue.all().filter('project =', project).order('fixed').order('created_date')
            
            # create the RSS feed
            rss = RSS2(
                title="Issues for %s on GitBug" % project.name,
                link="%s/%s/" % (settings.SYSTEM_URL, project.slug),
                description="",
                lastBuildDate=datetime.now()
            )

            # add an item for each issue
            for issue in issues:
                if issue.fixed: 
                    pubDate = issue.fixed_date
                    title = "%s (%s)" % (issue.name, "Fixed")
                else:
                    pubDate = issue.created_date
                    title = issue.name
                
                rss.items.append(
                    RSSItem(
                        title=title,
                        link="%s/projects%s" % (settings.SYSTEM_URL, issue.internal_url),
                        description=issue.html,
                        pubDate=pubDate
                    ))

            # get the xml
            output = rss.to_xml()

            memcache.add("project_%s_rss" % slug, output, 3600)
        # send the correct headers
        self.response.headers["Content-Type"] = "application/rss+xml; charset=utf8"
        self.response.out.write(output)

class ProjectDeleteHandler(BaseRequest):
    "Delete projects, including a confirmation page"
    def get(self, slug):
        "Display a confirmation page before deleting"
        # check the url has a trailing slash and add it if not
        if self.request.path[-1] != "/":
            self.redirect("%s/" % self.request.path, True)
            return
                        
        project = Project.all().filter('slug =', slug).fetch(1)[0]
        
        # if we don't have a user then throw
        # an unauthorised error
        user = users.get_current_user()
        if project.user == user or users.is_current_user_admin():
            owner = True
        else:
            self.render_403()
            return

        context = {
            'project': project,
            'owner': owner,
        }
        # calculate the template path
        output = self.render("project_delete.html", context)
        self.response.out.write(output)

    def post(self, slug):
        
        # if we don't have a user then throw
        # an unauthorised error
        user = users.get_current_user()
        if not user:
            self.render_403()
            return

        project = Project.all().filter('slug =', slug).fetch(1)[0]

        user = users.get_current_user()
        if project.user == user or users.is_current_user_admin():      
            try:
                logging.info("project deleted: %s" % project.name)
                # delete the project
                project.delete()
            except Exception, e:
                logging.error("error deleting project: %s" % e)

        # just head back to the home page, which should list you projects
        self.redirect("/")
        
class ProjectSettingsHandler(BaseRequest):
    "Dispay and allowing editing a few per project settings"
    def get(self, slug):
        # make sure we have a trailing slash
        if self.request.path[-1] != "/":
            self.redirect("%s/" % self.request.path, True)
            return

        try:
            project = Project.all().filter('slug =', slug).fetch(1)[0]
        except IndexError:
            self.render_404()
            return

        user = users.get_current_user()

        # check we have the permissions to be looking at settings
        if project.user == user or users.is_current_user_admin():
            owner = True
        else:
            self.render_403()
            return

        context = {
            'project': project,
            'owner': owner,
        }
        # calculate the template path
        output = self.render("project_settings.html", context)
        self.response.out.write(output)

    def post(self, slug):

        # if we don't have a user then throw
        # an unauthorised error
        user = users.get_current_user()
        if not user:
            self.render_403()
            return

        user = users.get_current_user()            

        project = Project.all().filter('slug =', slug).fetch(1)[0]

        if project.user == user:
            try:
                other_users = self.request.get("other_users")
                if other_users:
                    list_of_users = other_users.split(" ")
                    project.other_users = list_of_users
                else:
                    project.other_users = []
                    
                if self.request.get("url"):
                    url = self.request.get("url")
                    if not url[:7] == 'http://':
                        url = "http://%s" % url
                    if URL_RE.match(url):
                        project.url = url
                else:
                    project.url = None
                    
                if self.request.get("description"):
                    project.description = self.request.get("description")                
                else:
                    project.description = None
                project.put()
                logging.info("project modified: %s" % project.name)
            except db.BadValueError, e:
                logging.error("error modifiying project: %s" % e)

        self.redirect('/projects/%s/settings/' % project.slug)

class IssueHandler(BaseRequest):
    def get(self, project_slug, issue_slug):
        if self.request.path[-1] != "/":
            self.redirect("%s/" % self.request.path, True)
            return
            
        user = users.get_current_user()
        
        output = None
        if not user:
            output = get_cache("/%s/%s/" % (project_slug, issue_slug))
                    
        if output is None:
            try:
                issue = Issue.all().filter('internal_url =', "/%s/%s/" % (project_slug, issue_slug)).fetch(1)[0]
                issues = Issue.all().filter('project =', issue.project).filter('fixed =', False).fetch(10)
            except IndexError:
                self.render_404()
                return

            on_list = False
            try:
                if user.email() in issue.project.other_users:
                    on_list = True
            except:
                pass

            if issue.project.user == user or users.is_current_user_admin() or on_list:
                owner = True
            else:
                owner = False
            context = {
                'issue': issue,
                'issues': issues,
                'owner': owner,
            }
            # calculate the template path
            output = self.render("issue.html", context)

        if not user:
            memcache.add("/%s/%s/" % (project_slug, issue_slug), output, 60)   

        self.response.out.write(output)
    
    def post(self, project_slug, issue_slug):
        
        # if we don't have a user then throw
        # an unauthorised error
        user = users.get_current_user()
        if not user:
            self.render_403()
            return
        
        issue = Issue.all().filter('internal_url =', "/%s/%s/" % (project_slug, issue_slug)).fetch(1)[0]
        
        user = users.get_current_user()
        
        if issue.project.user == user:
        
            try:
                name = self.request.get("name")
                description = self.request.get("description")
                email = self.request.get("email")
                fixed = self.request.get("fixed")
                priority = self.request.get("priority")
                fixed_description = self.request.get("fixed_description")
        
                issue.name = name
                issue.description = description
                issue.priority = priority
                if email:
                    issue.email = email
                else:
                    issue.email = None
                issue.fixed = bool(fixed)
                if fixed:
                    issue.fixed_description = fixed_description
                else:
                    issue.fixed_description = None
    
                issue.put()
                logging.info("issue edited: %s in %s" % (issue.name, issue.project.name))
                
            except Exception, e:
                logging.info("error editing issue: %s" % e)

        self.redirect("/projects%s" % issue.internal_url)

class IssueJsonHandler(BaseRequest):
    def get(self, project_slug, issue_slug):

        output = get_cache("/%s/%s.json" % (project_slug, issue_slug))

        if output is None:
            issue = Issue.all().filter('internal_url =', "/%s/%s/" % (project_slug, issue_slug)).fetch(1)[0]

            if issue.fixed: 
                status = "Fixed"
            else:
                status = "Open"

            json = {
                'date': str(datetime.now())[0:19],
                'name': issue.name,
                'project': issue.project.name,
                'project_url': "%s/projects/%s" % (settings.SYSTEM_URL, issue.project.slug),
                'internal_url': "%s/projects/%s/" % (settings.SYSTEM_URL, issue.internal_url),
                'created_date': str(issue.created_date)[0:19],
                'description': issue.html,
                'status': status,
                'identifier': "#gitbug%s" % issue.identifier,
            }
            if issue.fixed and issue.fixed_description:
                json['fixed_description'] = issue.fixed_description

            output = simplejson.dumps(json)

            memcache.add("/%s/%s.json" % (project_slug, issue_slug), output, 3600)   
        self.response.headers["Content-Type"] = "application/javascript; charset=utf8"
        self.response.out.write(output)
        
class IssueDeleteHandler(BaseRequest):
    def get(self, project_slug, issue_slug):
        
        if self.request.path[-1] != "/":
            self.redirect("%s/" % self.request.path, True)
            return
        
        # if we don't have a user then throw
        # an unauthorised error
        user = users.get_current_user()
        if not user:
            self.render_403()
            return
            
        issue = Issue.all().filter('internal_url =', "/%s/%s/" % (project_slug, issue_slug)).fetch(1)[0]        
            
        if issue.project.user == user or users.is_current_user_admin():
            context = {
                'issue': issue,
                'owner': True,
            }
            output = self.render("issue_delete.html", context)
            self.response.out.write(output)
        else:
            self.render_403()
            return

    def post(self, project_slug, issue_slug):
        
        # if we don't have a user then throw
        # an unauthorised error
        user = users.get_current_user()
        if not user:
            self.render_403()
            return

        issue = Issue.all().filter('internal_url =', "/%s/%s/" % (project_slug, issue_slug)).fetch(1)[0]        

        user = users.get_current_user()
        if issue.project.user == user:
            try:
                logging.info("deleted issue: %s in %s" % (issue.name, issue.project.name))
                issue.delete()
            except Exception, e:
                logging.error("error deleting issue: %s" % e)
            self.redirect("/projects/%s" % issue.project.slug)
        else:
            self.render_403()
            return

class ProjectsHandler(BaseRequest):
    def get(self):
        if self.request.path[-1] != "/":
            self.redirect("%s/" % self.request.path, True)
            return
        
        user = users.get_current_user()
        output = None
        if not user:
            output = get_cache("projects")
        if output is None:
            projects = Project.all().order('-created_date').fetch(50)
            context = {
                'projects': projects,
            }
            # calculate the template path
            output = self.render("projects.html", context)
            if not user:
                memcache.add("projects", output, 3600)
        self.response.out.write(output)

    def post(self):
        
        # if we don't have a user then throw
        # an unauthorised error
        user = users.get_current_user()
        if not user:
            self.render_403()
            return
        
        name = self.request.get("name")
        
        # check we have a value
        if name:
            # then check we have a value which isn't just spaces
            if name.strip():
                if Project.all().filter('name =', name).count() == 0:
                    # we also need to check if we have something with the same slug
                    if Project.all().filter('slug =', slugify(unicode(name))).count() == 0:
                        try:
                            project = Project(
                                name=name,
                                user=users.get_current_user(),       
                            )
                            project.put()
                            logging.info("project added: %s" % project.name)
                        except db.BadValueError, e:
                            logging.error("error adding project: %s" % e)
        self.redirect('/')
        
class ProjectsJsonHandler(BaseRequest):
        def get(self):
            output = get_cache("projects_json")
            if output is None:
                projects = Project.all().order('-created_date').fetch(50)
                projects_data = {}

                for project in projects:
                    data = {
                        'internal_url': "%s/projects/%s/" % (settings.SYSTEM_URL, project.slug),
                        'created_date': str(project.created_date)[0:19],
                        'open_issues': project.open_issues.count(),
                        'closed_issues': project.closed_issues.count(),
                    }
                    if project.url:
                        data['external_url'] = project.url
                    projects_data[project.name] = data

                json = {
                    'date': str(datetime.now())[0:19],
                    'projects': projects_data,
                }

                output = simplejson.dumps(json)            
                memcache.add("projects_json", output, 3600)
            self.response.headers["Content-Type"] = "application/javascript; charset=utf8"
            self.response.out.write(output)

class ProjectsRssHandler(BaseRequest):
        def get(self):
            output = get_cache("projects_rss")
            if output is None:
                
                projects = Project.all().order('-created_date').fetch(20)
                rss = RSS2(
                    title="GitBug projects",
                    link="%s" % settings.SYSTEM_URL,
                    description="A list of the latest 20 projects on GitBug",
                    lastBuildDate=datetime.now()
                )

                for project in projects:
                    rss.items.append(
                        RSSItem(
                            title=project.name,
                            link="%s/projects/%s/" % (settings.SYSTEM_URL, project.slug),
                            description="",
                            pubDate=project.created_date
                        ))

                output = rss.to_xml()

                memcache.add("projects_rss", output, 3600)
            self.response.headers["Content-Type"] = "application/rss+xml; charset=utf8"
            self.response.out.write(output)

class WebHookHandler(BaseRequest):
    def post(self, slug):
        project = Project.all().filter('slug =', slug).fetch(1)[0]
        
        key = self.request.get("key")
        
        if key == project.key(): 
            try:
                payload = self.request.get("payload")
                representation = simplejson.loads(payload)
                commits = representation['commits']
                for commit in commits:
                    message = commit['message']
                    search = GITBUG.search(message)
                    if search:
                        identifier = search.group()[7:]                                
                        issue = Issue.all().filter('project =', project).filter('identifier =', int(identifier)).fetch(1)[0]
                        issue.fixed = True
                        issue.put()
                        logging.info("issue updated via webhook: %s in %s" % (issue.name, issue.project.name))
            except Exception, e:
                logging.error("webhook error: %s" % e)
        else:
            logging.info("webhook incorrect key provided: %s" % project.name)
            
        self.response.out.write("")

class NotFoundPageHandler(BaseRequest):
    def get(self):
        self.error(404)
        user = users.get_current_user()
        output = None
        if not user:
            output = get_cache("error404")
        if output is None:        
            output = self.render("404.html")
            if not user:
                memcache.add("error404", output, 3600)
        self.response.out.write(output)
        
class FaqPageHandler(BaseRequest):
    def get(self):
        if self.request.path[-1] != "/":
            self.redirect("%s/" % self.request.path, True)
            return
            
        user = users.get_current_user()
        output = None
        if not user:
            output = get_cache("faq")
        if output is None:        
            output = self.render("faq.html")
            if not user:
                memcache.add("faq", output, 3600)
        self.response.out.write(output)

class UploadHandler(webapp.RequestHandler):
    def post(self, slug):
        project = Project.all().filter('slug =', slug).fetch(1)[0]
        try:
            file = self.request.POST['file']

            f = DatastoreFile(data=file.value, mimetype=file.type, project=project, name=file.filename)
            f.put()

            url = "http://%s/file/%s/%d/%s" % (self.request.host, slug, f.key().id(), f.name)
        except Exception, e:
            logging.error("error uploading file: %s" % e)

        self.redirect("/projects/%s/" % slug)

class DownloadHandler(webapp.RequestHandler):
    def get(self, slug, id, filename):
        entity = DatastoreFile.get_by_id(int(id))
        self.response.headers['Content-Type'] = entity.mimetype
        self.response.out.write(entity.data)
                                        
def application():
    "Run the application"
    # wire up the views
    ROUTES = [
        ('/', Index),
        ('/projects.json$', ProjectsJsonHandler),
        ('/projects.rss$', ProjectsRssHandler),
        ('/projects/?$', ProjectsHandler),
        ('/projects/([A-Za-z0-9-]+)/hook/?$', WebHookHandler),
        ('/projects/([A-Za-z0-9-]+)/delete/?$', ProjectDeleteHandler),
        ('/projects/([A-Za-z0-9-]+)/settings/?$', ProjectSettingsHandler),
        ('/projects/([A-Za-z0-9-]+)/upload/?$', UploadHandler),
        ('/projects/([A-Za-z0-9-]+)/file/(\d+)/(.*)', DownloadHandler),
        ('/projects/([A-Za-z0-9-]+)/([A-Za-z0-9-]+).json$', IssueJsonHandler),
        ('/projects/([A-Za-z0-9-]+)/([A-Za-z0-9-]+)/?$', IssueHandler),
        ('/projects/([A-Za-z0-9-]+)/([A-Za-z0-9-]+)/delete/?$', IssueDeleteHandler),
        ('/projects/([A-Za-z0-9-]+).json$', ProjectJsonHandler),
        ('/projects/([A-Za-z0-9-]+).rss$', ProjectRssHandler),
        ('/projects/([A-Za-z0-9-]+)/?$', ProjectHandler),
        ('/faq/?$', FaqPageHandler),
        ('/.*', NotFoundPageHandler),
    ]
    application = webapp.WSGIApplication(ROUTES, debug=settings.DEBUG)
    return application

def main():
    "Run the application"
    run_wsgi_app(application())


if __name__ == '__main__':
    main()
