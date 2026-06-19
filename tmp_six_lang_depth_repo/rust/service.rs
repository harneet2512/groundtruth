trait Runner {
    fn run(&self, value: String) -> String;
}

struct UserService {
    count: i32,
}

impl Runner for UserService {
    fn run(&self, value: String) -> String {
        value
    }
}

impl UserService {
    fn validate(&mut self, value: Option<String>) -> Result<String, String> {
        self.count = self.count + 1;
        match value {
            Some(v) => Ok(self.run(v)),
            None => Err("missing".to_string()),
        }
    }
}

fn entry(value: Option<String>) -> Result<String, String> {
    let mut service = UserService { count: 0 };
    service.validate(value)
}
