import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.Properties;

public final class JdbcDemoClient {
    private JdbcDemoClient() {
    }

    private static void drain(ResultSet resultSet) throws SQLException {
        int columnCount = resultSet.getMetaData().getColumnCount();
        while (resultSet.next()) {
            for (int index = 1; index <= columnCount; index += 1) {
                resultSet.getObject(index);
            }
        }
    }

    private static void runPreparedQuery(PreparedStatement statement, String name) throws SQLException {
        statement.setString(1, name);
        try (ResultSet resultSet = statement.executeQuery()) {
            drain(resultSet);
        }
        statement.clearParameters();
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            throw new IllegalArgumentException("usage: JdbcDemoClient <jdbc-url> <user> [password]");
        }

        Class.forName("org.postgresql.Driver");

        String jdbcUrl = args[0];
        String user = args[1];
        String password = args.length >= 3 ? args[2] : "";

        Properties properties = new Properties();
        properties.setProperty("user", user);
        if (!password.isEmpty()) {
            properties.setProperty("password", password);
        }

        try (Connection connection = DriverManager.getConnection(jdbcUrl, properties)) {
            try (Statement statement = connection.createStatement()) {
                statement.execute("create table if not exists demo(id serial primary key, name text not null)");
                statement.execute("truncate table demo restart identity");
                statement.execute("insert into demo(name) values ('alice'), ('bob'), ('carol')");
                try (ResultSet resultSet = statement.executeQuery("select count(*) as total_rows from demo")) {
                    drain(resultSet);
                }
            }

            try (PreparedStatement prepared =
                connection.prepareStatement("select id, name from demo where name = ? order by id")) {
                runPreparedQuery(prepared, "alice");
                runPreparedQuery(prepared, "bob");

                try (Statement statement = connection.createStatement()) {
                    statement.execute("begin");
                }
                runPreparedQuery(prepared, "carol");
                try (Statement statement = connection.createStatement()) {
                    statement.execute("commit");
                }
            }
        }
    }
}
