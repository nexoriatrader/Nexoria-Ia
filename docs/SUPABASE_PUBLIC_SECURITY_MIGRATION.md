# Supabase NEXORA — migração de segurança do esquema public

Revisão de 17/11/2026. Projeto `xpkulrzwkecnwvxzymyq`.
Objetivo: impedir acesso via Data API sob `anon` e `authenticated`
a dados de sessões, usuários, contratos, resultados e pagamentos.

## Verificação antes da mudança
- 27 tabelas no esquema `public` sem RLS;
- 27 com SELECT de `anon`, 27 com INSERT de `authenticated`;
- tabelas públicas pertencem ao papel Postgres `postgres`;
- sessões SQL ativas identificadas com o papel `postgres`/Supavisor;
- backend usa SQLAlchemy por `DATABASE_URL` e não depende do Data API para os registros da aplicação.

## SQL de proteção (sem exclusão de dados)
```sql
BEGIN;
SET LOCAL lock_timeout = '5s';

DO $$
DECLARE item record;
BEGIN
  FOR item IN
    SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', item.tablename);
  END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
  REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM anon, authenticated;

COMMIT;
```

A proteção não cria política pública de acesso. O papel `postgres` é
proprietário das tabelas, tem `BYPASSRLS` e mantém seus privilégios de servidor.
Se um futuro front-end usar a API REST do Supabase, será necessário introduzir
um esquema de API próprio ou políticas de acesso mínimo após uma auditoria.

## Verificação automática
```sql
SELECT COUNT(*) AS total_tables,
 COUNT(*) FILTER(WHERE rowsecurity) AS rls_enabled,
 COUNT(*) FILTER(WHERE has_table_privilege('anon',
   format('%I.%I',schemaname,tablename),'SELECT')) AS anon_can_read,
 COUNT(*) FILTER(WHERE has_table_privilege('authenticated',
   format('%I.%I',schemaname,tablename),'INSERT')) AS authenticated_can_write
FROM pg_tables WHERE schemaname='public';
```
Esperado: 27/27 protegidas, ambos os contadores de permissões = 0.
Verificar também permissões padrão, advisor de segurança e acessos ao Render.

## Plano de reversão emergencial
Se for comprovada interrupção causada por essa migração, restaurar temporariamente
as antigas permissões de tabela/sequência com comandos GRANT ALL apropriados
e desabilitar RLS apenas em ambiente de contingência, após avaliar o risco
de reabrir acesso anônimo. Preferir corrigir o cliente para usar o backend
SQLAlchemy como configurado, mantendo RLS.

**Importante:** isso é uma reversão de permissões, não uma restauração de dados.
Não substitui backup de banco, nem validação de autenticação e recuperação em desastre.
