# NEXORA — proteção do banco Supabase aplicada (17/11/2026)

Data de auditoria: 2026-11-17. Projeto: xpkulrzwkecnwvxzymyq.

## Evidência
- Há 27 tabelas em `public`, todas com RLS desativado.
- Todas as 27 concedem leitura a `anon` e `authenticated`; as ACLs vistas incluem escrita.
- O backend atual usa SQLAlchemy/psycopg por `DATABASE_URL`, não um cliente Supabase JS.
- O proprietário das tabelas conferidas é `postgres`; esta implementação depende do uso do papel proprietário no backend. CONFIRMAR a identidade de conexão efetiva antes da aplicação.

## Histórico da migração aplicada

Migração de produção registrada no Supabase como `secure_nexora_public_rls_revoke_client_grants_20261117`, versão `20261117181651`. **Já aplicada**. O comando registrado abaixo resume os mecanismos implantados; consulte `SUPABASE_PUBLIC_SECURITY_MIGRATION.md` para o SQL de produção e plano de contingência. A validação com login real no site e a revisão do Data API ainda são pendências.

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
**Verificação pós-migração confirmada:** 27/27 tabelas com RLS, 0 com SELECT por `anon` e 0 com SELECT/INSERT por `authenticated`. Registros e direitos do papel `postgres` preservados.

Critério de aprovação: `rls_enabled = total`, `anon_can_select = 0`,
`authenticated_can_select = 0`, sem regressão no backend. Reexecutar Supabase
Security Advisors e testes de acesso negativo por papéis sem privilégios. O advisor retornou apenas `rls_enabled_no_policy` (INFO), esperado para tabelas privadas sem políticas públicas.
Se algum futuro recurso exigir Data API, adicionar GRANT mínimo e política RLS
específica com autenticação e verificação de propriedade; nunca `USING (true)`
em tabelas de usuários/ordens.

Fontes: https://supabase.com/docs/guides/database/postgres/row-level-security
e https://supabase.com/docs/guides/api/securing-your-api
