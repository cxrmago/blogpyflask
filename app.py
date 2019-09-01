import datetime
import functools
import os
import re
import urllib

from flask import (Flask, flash, Markup, redirect, render_template, request,
                   Response, session, url_for)
from markdown import markdown
from markdown.extensions.codehilite import CodeHiliteExtension
from markdown.extensions.extra import ExtraExtension
from micawber import bootstrap_basic, parse_html
from micawber.cache import Cache as OEmbedCache
from peewee import *
from playhouse.flask_utils import FlaskDB, get_object_or_404, object_list
from playhouse.sqlite_ext import *


# Variables de configuracion para el Blog.

#Debes considerar una clave mas compleja que la suministrada, esta es utilizada para logearte
ADMIN_PASSWORD = 'secret'
APP_DIR = os.path.dirname(os.path.realpath(__file__))

# el modulo playhouse.flask_utils.FlaskDB  es usado para aceptar direcciones pertenecienmtes a BD.
DATABASE = 'sqliteext:///%s' % os.path.join(APP_DIR, 'blog.db')
DEBUG = False

#La llave secreta es usada internamente  por flask para encriptar la informacion de la sesion de cada cookie
SECRET_KEY = 'shhh, secret!'

# Esta funcion es usada por micawber, la cual define un ancho max para el sitio
SITE_WIDTH = 800


# Create a Flask WSGI app and configure it using values from the module.
app = Flask(__name__)
app.config.from_object(__name__)

# FlaskDB is a wrapper for a peewee database that sets up pre/post-request
# hooks for managing database connect
# FlaskDB es un organizador para las conexciones con la base de datos de peewee 
flask_db = FlaskDB(app)

#La database es nuestra actual base de datos [linea 26]
database = flask_db.database

# micawber con la configuracion por defecto de oembed_providers(Youtube, Flickr, etc)
# usa memoria cache con la cual podemos agregar mas de un Video al mismo tiempo, sin tener que abrir otras conexiones
oembed_providers = bootstrap_basic(OEmbedCache())

#[MODELO]
#Aqui definiremos  el contenido y las funciones de nuestra base de datos
#titulo, tag, slug, content, published, timestamp

#Definicion de las variables para las columnas 
class Entry(flask_db.Model):
    title = CharField()
    slug = CharField(unique=True)
    content = TextField()
    published = BooleanField(index=True)
    timestamp = DateTimeField(default=datetime.datetime.now, index=True)

    @property
    def html_content(self):
        """
        Generate HTML representation of the markdown-formatted blog entry,
        and also convert any media URLs into rich media objects such as video
        players or images.
	Genera el Html con las nuevas entradas y transmite las  medias-url en un reproductor de video del navegador
        """
	#css_class highlight -> adornar el marco del reproductor
        hilite = CodeHiliteExtension(linenums=False, css_class='highlight')
        extras = ExtraExtension()
        markdown_content = markdown(self.content, extensions=[hilite, extras])
        oembed_content = parse_html(
            markdown_content,
            oembed_providers,
            urlize_all=True,
            maxwidth=app.config['SITE_WIDTH'])
        return Markup(oembed_content)

    def save(self, *args, **kwargs):
	# Genera una url-amigable(.io/enlace_amistoso) para la publicacion
        if not self.slug:
            self.slug = re.sub('[^\w]+', '-', self.title.lower()).strip('-')
        ret = super(Entry, self).save(*args, **kwargs)

        # almacena la busqueda
        self.update_search_index()
        return ret

    def update_search_index(self):
	# Crea una columna en la tabla FTSEntry con el contenido del post, esto
	# nos permitira usar la funcion de sqlite para hacer nuestras busquedas
        exists = (FTSEntry
                  .select(FTSEntry.docid)
                  .where(FTSEntry.docid == self.id)
                  .exists())
        content = '\n'.join((self.title, self.content))
        if exists:
            (FTSEntry
             .update({FTSEntry.content: content})
             .where(FTSEntry.docid == self.id)
             .execute())
        else:
            FTSEntry.insert({
                FTSEntry.docid: self.id,
                FTSEntry.content: content}).execute()
	# los metodos de la clase buscan, muestran, actualizan.
	# son sentencias sql con condiciones para los datos 
    @classmethod
    def public(cls):
        return Entry.select().where(Entry.published == True)

    @classmethod
    def drafts(cls):
        return Entry.select().where(Entry.published == False)

    @classmethod
    def search(cls, query):
        words = [word.strip() for word in query.split() if word.strip()]
        if not words:
            # Retorna una busqueda vacia.
            return Entry.noop()
        else:
            search = ' '.join(words)

	# hace una busqueda del texto introducido en las tablas
        return (Entry
                .select(Entry, FTSEntry.rank().alias('score'))
                .join(FTSEntry, on=(Entry.id == FTSEntry.docid))
                .where(
                    FTSEntry.match(search) &
                    (Entry.published == True))
                .order_by(SQL('score')))

#FTSEntry los metadatos del contenido
class FTSEntry(FTSModel):
    content = TextField()

    class Meta:
        database = database

#Funciones del Blog
def login_required(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        if session.get('logged_in'):
            return fn(*args, **kwargs)
        return redirect(url_for('login', next=request.path))
    return inner

# app.route define la ruta de la pagina del sitio con los metodos que necesitara 
@app.route('/login/', methods=['GET', 'POST'])
def login():
    next_url = request.args.get('next') or request.form.get('next')
    if request.method == 'POST' and request.form.get('password'):
        password = request.form.get('password')
	# Para Hacer : Si usa un hash unidireccional, también debe hacer hash el mensaje enviado por el usuario
        # haga la comparación en las versiones hash. Es decir busca una forma de mejorar la seguridad 
        if password == app.config['ADMIN_PASSWORD']:
            session['logged_in'] = True
            session.permanent = True  # Usar Cookie para guardar sesion
            flash('You are now logged in.', 'success')
            return redirect(next_url or url_for('index'))
        else:
            flash('Incorrect password.', 'danger')
    return render_template('login.html', next_url=next_url)

@app.route('/logout/', methods=['GET', 'POST'])
def logout():
    if request.method == 'POST':
        session.clear()
        return redirect(url_for('login'))
    return render_template('logout.html')

@app.route('/')
def index():
    search_query = request.args.get('q')
    if search_query:
        query = Entry.search(search_query)
    else:
        query = Entry.public().order_by(Entry.timestamp.desc())

    # el object_list tomara la busqueda y  manejara un orden de 20 post x pag
    # the docs:
    # http://docs.peewee-orm.com/en/latest/peewee/playhouse.html#object_list
    return object_list(
        'index.html',
        query,
        search=search_query,
        check_bounds=False)

def _create_or_edit(entry, template):
    if request.method == 'POST':
        entry.title = request.form.get('title') or ''
        entry.content = request.form.get('content') or ''
        entry.published = request.form.get('published') or False
        if not (entry.title and entry.content):
            flash('Title and Content are required.', 'danger')
        else:
	    # encapsula el llamado para salvarlo en la transaccion, entonces
	    # podemos volver de nuevo atras en caso de error
            try:
                with database.atomic():
                    entry.save()
            except IntegrityError:
                flash('Error: this title is already in use.', 'danger')
            else:
                flash('Entry saved successfully.', 'success')
                if entry.published:
                    return redirect(url_for('detail', slug=entry.slug))
                else:
                    return redirect(url_for('edit', slug=entry.slug))

    return render_template(template, entry=entry)

#Vistas
@app.route('/create/', methods=['GET', 'POST'])
@login_required
def create():
    return _create_or_edit(Entry(title='', content=''), 'create.html')

@app.route('/drafts/')
@login_required
def drafts():
    query = Entry.drafts().order_by(Entry.timestamp.desc())
    return object_list('index.html', query, check_bounds=False)

@app.route('/<slug>/')
def detail(slug):
    if session.get('logged_in'):
        query = Entry.select()
    else:
        query = Entry.public()
    entry = get_object_or_404(query, Entry.slug == slug)
    return render_template('detail.html', entry=entry)

@app.route('/<slug>/edit/', methods=['GET', 'POST'])
@login_required
def edit(slug):
    entry = get_object_or_404(Entry, Entry.slug == slug)
    return _create_or_edit(entry, 'edit.html')

@app.template_filter('clean_querystring')
def clean_querystring(request_args, *keys_to_remove, **new_values):
    # el filtro de plantillas(templates) en la paginacion(includes/pagination.html)
    # este filtro tomara la actual URL y nos permitira preservar los argumentos
    # en la cadena de busqueda mientras se remplaza cualquier dato que necesite
    # ser sobreescribido /?q=search+query&page=2, /?q=search+query&page=3
    querystring = dict((key, value) for key, value in request_args.items())
    for key in keys_to_remove:
        querystring.pop(key, None)
    querystring.update(new_values)
    return urllib.urlencode(querystring)

@app.errorhandler(404)
def not_found(exc):
    return Response('<h3>Not found</h3>'), 404

#Funcion principal
def main():
    database.create_tables([Entry, FTSEntry], safe=True)
    app.run(debug=True)

if __name__ == '__main__':
    main()
