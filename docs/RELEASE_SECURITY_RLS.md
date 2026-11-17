# NEXORA — proteção do banco Supabase antes do lançamento

Data de auditoria: 2026-11-17. Projeto: xpkulrzwkecnwvxzymyq.

## Evidência
- Há 27 tabelas em `public`, todas com RLS desativado.
- Todas as 27 concedem leitura a `anon` e `authenticated`; as ACLs vistas incluem escrita.
- O backend atual usa SQLAlchemy/psycopg por `DATABASE_URL`, não um cliente Supabase JS.
- O proprietário das tabelas conferidas é `postgres`; esta implementação depende do uso do papel proprietário no backend. CONFIRMAR a identidade de conexão efetiva antes da aplicação.

## Migração proposta (NÃO APLICADA)

Atenção: executar inicialmente em ambiente de homologação e testar OAuth, dashboard,
edição de preferências, histórico, assinatura e operação DEMO. Aplicar em produção apenas
após validar que o backend conecta como proprietário e que nenhum cliente usa PostgREST
com papel `anon` ou `authenticated`. As tabelas são dados privados de servidor,
portanto não devem expor políticas públicas. A operação REAL deve continuar desligada.

```sql
BEGIN;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
DO $$
DECLARE item record;
BEGIN
  FOR item IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', item.tablename);
  END LOOP;
END $$;
-- Também revisar privilégios padrão de novas tabelas; configurações globais podem
-- ter prioridade e não devem ser modificadas sem análise de dependências.
COMMIT;
```

## Verificação após migração
```sql
SELECT count(*) AS total,
       count(*) FILTER (WHERE rowsecurity) AS rls_enabled,
       count(*) FILTER (
         WHERE has_table_privilege('anon',format('%I.%I',schemaname,tablename),'SELECT')
       ) AS anon_can_select,
       count(*) FILTER (
         WHERE has_table_privilege('authenticated',format('%I.%I',schemaname,tablename),'SELECT')
       ) AS authenticated_can_select
FROM pg_tables WHERE schemaname = 'public';
```
Critério de aprovação: `rls_enabled = total`, `anon_can_select = 0`,
`authenticated_can_select = 0`, sem regressão no backend. Reexecutar Supabase
Security Advisors e testes de acesso negativo por papéis sem privilégios.
Se algum futuro recurso exigir Data API, adicionar GRANT mínimo e política RLS
específica com autenticação e verificação de propriedade; nunca `USING (true)`
em tabelas de usuários/ordens.

Fontes: https://supabase.com/docs/guides/database/postgres/row-level-security
e https://supabase.com/docs/guides/api/securing-your-api
