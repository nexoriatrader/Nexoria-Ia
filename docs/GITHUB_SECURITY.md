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
1. **Não altere a visibilidade antes de auditar o histórico e logs.**
   A conta atingiu a franquia GitHub Actions de repositórios privados.
   Em repositórios públicos, executores padrão podem ser gratuitos, mas
   toda a árvore, o histórico alcançável, os PRs e os logs podem ficar visíveis.
   Esta vantagem de cobrança NÃO é uma liberação de segurança.
2. Configure proteção de `main` e exija revisão por PR, quando disponível
   no plano, em Settings > Branches / Rules:
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

## Solicitação de visibilidade pública — pré-condições (22/11/2026)

- A visibilidade do repositório `nexoriatrader/Nexoria-Ia` **continua privada**.
  A conexão atual ao GitHub permite atualizar arquivos, mas não alterar
  a visibilidade administrativa da conta.
- A PR #75 contém o auditor de objetos históricos
  `scripts/check_repository_history.py`, testes de regressão e o workflow
  atualizado para `fetch-depth: 0`. Nenhum desses testes foi executado
  pelo GitHub Actions enquanto os executores não forem atribuídos.
- Uma inspeção pontual de 30 arquivos sensíveis no HEAD da PR #75 não
  encontrou padrões evidentes das credenciais procuradas. **Isso não é uma
  auditoria completa, não inclui histórico, anexos ou logs, e não autoriza
  tornar pública a plataforma**.
- Antes de qualquer abertura, executar **num checkout local completo**
  `python3 scripts/check_repository_security.py` e
  `python3 scripts/check_repository_history.py`, conferir todos os refs,
  arquivos anexos e logs passados, e analisar padrões não reconhecidos
  pelo scanner. Não basta excluir um arquivo do último commit.
- Se houver qualquer indício de segredo real, **revogar/rotacionar**
  primeiro e considerar repositório público limpo e separado para testes
  apenas de pesquisa educacional. A publicação direta do histórico anterior
  expõe materiais antigos e não pode ser desfeita pela reversão de
  visibilidade.
- A `main` segue conectada à produção Render. Não permitir merge/deploy,
  alterações em controles de execução REAL ou mudanças nos mecanismos
  DEMO como parte do processo de auditoria.

## Escopo e limitações da auditoria automática
A checagem em HEAD reconhece padrões fortes de credenciais e arquivos
sensíveis atuais. A PR #75 acrescenta checagem de objetos históricos **alcançáveis
nas referências buscadas**, mas não reconhece tokens opacos sem formato,
não inspeciona refs apagadas, forks, anexos, logs ou segredos externos.
Nenhum scanner substitui revisão humana, rotação de segredos comprometidos
nem regras obrigatórias de branch e PR.

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
