#!/bin/bash
# Pull pre-built OpenHands agent-server Docker images for SWE-bench instances
TAG_PREFIX="62c2e7c-sweb.eval.x86_64"
REPO="ghcr.io/openhands/eval-agent-server"

INSTANCES="
django_1776_django-10914-source-minimal
django_1776_django-10924-source-minimal
django_1776_django-11001-source-minimal
django_1776_django-11019-source-minimal
django_1776_django-11039-source-minimal
django_1776_django-11049-source-minimal
django_1776_django-11099-source-minimal
django_1776_django-11133-source-minimal
django_1776_django-16379-source-minimal
scikit-learn_1776_scikit-learn-13779-source-minimal
scikit-learn_1776_scikit-learn-25500-source-minimal
sympy_1776_sympy-18189-source-minimal
sympy_1776_sympy-13146-source-minimal
sympy_1776_sympy-12171-source-minimal
psf_1776_requests-2317-source-minimal
pytest-dev_1776_pytest-6116-source-minimal
astropy_1776_astropy-7746-source-minimal
astropy_1776_astropy-12907-source-minimal
"

for inst in $INSTANCES; do
    full_tag="${REPO}:${TAG_PREFIX}.${inst}"
    if docker image inspect "$full_tag" > /dev/null 2>&1; then
        echo "EXISTS: $inst"
    else
        echo "PULLING: $inst"
        docker pull "$full_tag" 2>&1 | tail -3
    fi
done
echo "All done"
