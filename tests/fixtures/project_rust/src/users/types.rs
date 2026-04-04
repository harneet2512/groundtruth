pub struct User {
    pub id: i64,
    pub email: String,
    pub name: String,
    pub password_hash: String,
}

impl User {
    pub fn new(id: i64, email: String, name: String, password_hash: String) -> Self {
        User { id, email, name, password_hash }
    }
}
