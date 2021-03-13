#!/bin/sh

# WAIT_FOR is a list of <hostname>:<port> that seprated by comma

WAIT_FOR=$(sh -c 'echo "${WAIT_FOR?false}"')
printf "\033[0;32m > Wait for server ${WAIT_FOR} to ready ...\n"
services=`echo $WAIT_FOR | awk -F ',' '{ s = $1; for (i = 2; i <= NF; i++) s = s "\n"$i; print s; }'`

for service in $services
do
  count=100;
  while [[ $count -ne 0 ]] ; do
      echo "."
      ping -c 1 "${service}" &> /dev/null
      rc=$?
      if [[ $rc -eq 0 ]] ; then
          break
      else
          sleep 5
      fi
      count=$((count - 1));
  done
done

set -e
if [[ $rc -eq 0 ]] || [[ ${#services[*]} -eq 0 ]]; then
    printf "\033[0;32m > ${WAIT_FOR} is ready. \n"
else
    printf "\033[0;31m > ${WAIT_FOR} not ready!! \n"
    exit 1
fi

printf "\033[0;32m > Start Server ...\n"
printf "\033[0m\n"

circusd /etc/circus.ini
