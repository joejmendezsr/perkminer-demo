from app import app, db, User, Role, UserRoles, bcrypt

with app.app_context():
    # --- Roles to create ---
    role_names = [
        "approve_reject_listings",
        "finance",
        "feedback_moderation",
        "customer_support"
    ]

    # --- Create roles if missing ---
    roles = {}
    for name in role_names:
        role = Role.query.filter_by(name=name).first()
        if not role:
            role = Role(name=name)
            db.session.add(role)
            print(f"Added role: {name}")
        roles[name] = role
    db.session.commit()

    # --- Create demo admin users if they don't exist ---
    admins = [
        {
            "email": "admin1@perkminer.com",
            "password": "admin1secure",  # Change to strong password in production
            "role_names": [
                "approve_reject_listings", "feedback_moderation", "customer_support"
            ]
        },
        {
            "email": "finance1@perkminer.com",
            "password": "finance1secure",  # Change to strong password in production
            "role_names": ["finance"]
        }
    ]

    for admin in admins:
        user = User.query.filter_by(email=admin["email"]).first()
        if not user:
            hashed_pw = bcrypt.generate_password_hash(admin["password"]).decode("utf-8")
            user = User(
                email=admin["email"],
                password=hashed_pw,
                email_confirmed=True  # Or False if you want to do email verify
            )
            db.session.add(user)
            db.session.commit()
            print(f"Created user: {admin['email']}")
        # Now assign roles
        for role_name in admin["role_names"]:
            role = Role.query.filter_by(name=role_name).first()
            if role not in user.roles:
                user.roles.append(role)
                print(f"Granted {role_name} to {admin['email']}")
        db.session.commit()

    # --- Assign roles to existing user(s) by email ---
    target_email = "joejmendez@gmail.com"  # Change as needed
    target_roles = ["finance"]  # Add any roles you want, e.g. ["finance", "customer_support"]

    user = User.query.filter_by(email=target_email).first()
    if user:
        for role_name in target_roles:
            role = Role.query.filter_by(name=role_name).first()
            if role and role not in user.roles:
                user.roles.append(role)
                print(f"Granted {role_name} to {target_email}")
        db.session.commit()
    else:
        print(f"User {target_email} not found; cannot assign roles.")

    print("Seeding complete!")