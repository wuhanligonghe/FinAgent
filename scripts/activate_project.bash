# Source from ~/.bashrc to activate this project's virtual environment.
_FINAGENT_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

_finagent_auto_activate() {
    local previous_status=$?
    local project_venv="$_FINAGENT_PROJECT_ROOT/.venv"

    case "$PWD/" in
        "$_FINAGENT_PROJECT_ROOT/"*)
            if [[ ${VIRTUAL_ENV-} != "$project_venv" && -f "$project_venv/bin/activate" ]]; then
                source "$project_venv/bin/activate"
                _FINAGENT_AUTO_VENV="$project_venv"
            fi
            ;;
        *)
            if [[ -n ${_FINAGENT_AUTO_VENV-} ]]; then
                if [[ ${VIRTUAL_ENV-} == "$_FINAGENT_AUTO_VENV" ]] && declare -F deactivate >/dev/null; then
                    deactivate
                fi
                unset _FINAGENT_AUTO_VENV
            fi
            ;;
    esac
    return "$previous_status"
}

if [[ ${PROMPT_COMMAND[*]-} != *"_finagent_auto_activate"* ]]; then
    if [[ $(declare -p PROMPT_COMMAND 2>/dev/null) == "declare -a "* ]]; then
        PROMPT_COMMAND+=(_finagent_auto_activate)
    else
        PROMPT_COMMAND="${PROMPT_COMMAND:+${PROMPT_COMMAND}; }_finagent_auto_activate"
    fi
fi

_finagent_auto_activate
