"""Declarative base config for SQLAlchemy 2.0 ORM models.

# SQLAlchemy is the standard Object-Relational Mapper (ORM) and database toolkit for python. It acts as a translation layer between python code and SQL database engines.add()
# Instead of writing raw SQL strings inside python, SQLAlchemy allows you to define database tables as python classes and query data using standard python objects and methods.

Defining the base.py to establish a central registry for database metadata, and we create individual ORM models (task.py, artifact.py, log.py) to map PostgreSQL relational tables directly into strongly-typed Python objects.

"""

from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    """Abstract declarative base class inherited by all ORM entity models. 
    
    Provides a shared metadata registry for schema generation and Alembic migrations.
    """
    pass 