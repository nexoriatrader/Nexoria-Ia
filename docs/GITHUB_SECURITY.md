# Segurança GitHub — NEXORA IA

Auditoria: 17/11/2026. Repositório: `nexoriatrader/Nexoria-Ia`.

## Estado verificado
- Visibilidade: **private** (confirmada na API GitHub).
- Branch `main`: **sem branch protection** (confirmada na API).
- API de Rulesets: devolveu 403 exigindo GitHub Pro ou repositório público.
- Correções para lançamento estão na PR #31, branch `fix/release-readiness-20261117`.
- Auditoria automática de padrões de segredo em arquivos HEAD versionados implementada
  via `.github/workflows/repository-security.yml` e
  `scripts/check_repository_security.py`. Não substitui revisão de histórico.
- `.gitignore` exclui `.env`, bancos locais, caches e arquivos SQLite.
- O `DERIV_CLIENT_ID` é identificador público OAuth, **não** senha nem token negociador.

## Antes do lançamento (ações de conta)
1. **Mantenha o repositório privado.** Não use a opção de transformá-lo
   em público para contornar a restrição do GitHub Free.
2. Configure **GitHub Pro** (ou um plano com regras de proteção para repos
   privados) e depois proteja `main` em Settings > Branches / Rules:
   exigir PR antes de merge, checagens `NEXORA Python tests`,
   `NEXORA progression safety regression`, `NEXORA repository security audit`,
   impedir force-push e exclusão de branch, e impedir bypass não autorizado.
3. Exigir autenticação de dois fatores ou passkey na conta GitHub; conferir
   sessões, terceiros autorizados, SSH keys e deploy keys sem uso; usar
   escopos mínimos no Render e nos conectores.
4. Manter `DATABASE_URL`, `NEXORA_FERNET_KEY`, chaves Stripe, Deriv
   e chaves Supabase somente em variáveis de ambiente privadas do servidor.
   Nunca inserir esses valores no HTML, nos logs ou nos comentários de PR.
5. Verificar **Security > Secret scanning / Dependabot / Code scanning** e
   habilitar todos os recursos disponíveis no plano.
6. Revisar histórico Git completo e arquivos anexados a Issues/PRs.
   Um segredo comprometido deve ser **revogado/rotacionado** mesmo que
   o arquivo seja apagado e o repositório seja privado.
7. Usar PR e revisão para mudanças; evitar commits diretos em `main` enquanto
   não houver regras obrigatórias. `main` está ligada ao auto deploy do Render.

## Escopo e limitações da auditoria automática
A checagem atual reconhece padrões fortes de credenciais e arquivos sensíveis
rastreados em HEAD. Ela não testa tokens Deriv opacos sem prefixo conhecido,
não examina todo o histórico e não bloqueia commits diretos em `main` antes
do push quando a branch não é protegida. Para uma proteção completa, são
necessárias regras de branch/PR e revisão de incidentes e segredos.

## NEXORA no navegador
O repositório ser privado não impede que o HTML, CSS e JavaScript enviados
ao navegador sejam vistos por visitantes do site. Lógica de negociação,
permissões, gestão de risco e credenciais devem permanecer exclusivamente
no backend e ser verificadas sempre pelo servidor.

## Publicação controlada
A PR #31 agora também exige `NEXORA_REAL_RELEASE_APPROVED=true`
**além de** `NEXORA_REAL_AUTOPILOT_ENABLED=true` para expor
operações reais. É uma barreira técnica para deploy; não substitui
verificação regulatória, autenticação da corretora, consentimento individual
ou testes de execução. Em homologação e primeiro rollout mantenha ambas
explicitamente em `false`.
