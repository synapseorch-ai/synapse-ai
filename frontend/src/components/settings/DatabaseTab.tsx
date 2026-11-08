import { Database } from 'lucide-react';

interface DatabaseTabProps {
    sqlConnectionString: string;
    setSqlConnectionString: (v: string) => void;
    onSave: () => void;
}

export const DatabaseTab = ({ sqlConnectionString, setSqlConnectionString, onSave }: DatabaseTabProps) => (
    <div className="space-y-8">
        <div className="mb-4">
            <h3 className="text-lg font-bold text-white flex items-center gap-2">
                <Database className="h-5 w-5" />
                SQL Database Connection
            </h3>
            <p className="text-text-faint text-sm mt-1">
                Connect your agent to a SQL database (PostgreSQL, MySQL, SQLite) to enable business intelligence capabilities.
            </p>
        </div>

        <div className="space-y-4">
            <div className="space-y-2">
                <label htmlFor="database-connection-string-sqlalchemy" className="text-xs uppercase font-bold text-text-faint tracking-wider">Connection String (SQLAlchemy URL)</label>
                <input
                    id="database-connection-string-sqlalchemy"
                    type="password"
                    value={sqlConnectionString}
                    onChange={(e) => setSqlConnectionString(e.target.value)}
                    className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono rounded-md"
                    placeholder="postgresql://user:password@localhost:5432/dbname"
                />
                <p className="text-xs text-text-faint">
                    Format: <code className="font-code">dialect+driver://username:password@host:port/database</code><br />
                    Examples:<br />
                    - Postgres: <code className="font-code">postgresql://scott:tiger@localhost/test</code><br />
                    - MySQL: <code className="font-code">mysql+pymysql://user:pass@localhost/foo</code><br />
                    - SQLite: <code className="font-code">sqlite:///foo.db</code>
                </p>
            </div>

            <div className="p-4 bg-zinc-900/50 border border-zinc-800 text-xs text-zinc-400 rounded-md">
                <strong>Security Note:</strong> The agent will have access to execute queries. Use a read-only user if possible.
            </div>
        </div>
        <div className="pt-4 flex justify-end">
            <button
                onClick={onSave}
                className="px-6 py-2.5 text-sm font-bold bg-accent text-accent-fg hover:bg-accent-hover transition-all shadow-lg"
            >
                Save Changes
            </button>
        </div>
    </div>
);
