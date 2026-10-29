# Add these lines inside your existing `class User(db.Model, UserMixin):` (anywhere with the other columns).
# The database columns already exist (migration_docuseries.sql); this just tells SQLAlchemy about them.
# Do NOT add a docuseries_role field: the role is a row in your Role table, checked with has_role('docuseries').

    release_form = db.Column(db.Text)                                                # storage key of the signed Talent Release Form
    docuseries_terms = db.Column(db.Boolean, default=False, nullable=False)          # agreed to the docuseries terms
    docuseries_terms_at = db.Column(db.DateTime(timezone=True))                      # when they agreed
    docuseries_welcomed = db.Column(db.Boolean, default=False, nullable=False)       # one-time "Congratulations" banner shown
