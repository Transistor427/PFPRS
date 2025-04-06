#!/bin/bash
SD_PATH=~/printer_data/gcodes/
FILE_NAME=restore.gcode


cat ${2} > /tmp/plrtmpA.$$

cat /tmp/plrtmpA.$$ | sed -e '1,/;Z:'${1}'/ d' | > ${SD_PATH}/${FILE_NAME}

echo '_START_PRINT_RESTORE' >> ${SD_PATH}/${FILE_NAME}

#BG_EX=`tac /tmp/plrtmpA.$$ | sed -e '/ Z'${1}'[^0-9]*$/q' | tac | tail -n+2 | sed -e '/ Z[0-9]/ q' | tac | sed -e '/ E[0-9]/ q' | sed -ne 's/.* E\([^ ]*\)/G92 E\1/p'`

# If we failed to match an extrusion command (allowing us to correctly set the E axis) prior to the matched layer height, then simply set the E axis to the first E value present in the resemued gcode.  This avoids extruding a huge blod on resume, and/or max extrusion errors.
#if [ "${BG_EX}" = "" ]; then
# BG_EX=`tac /tmp/plrtmpA.$$ | sed -e '/ Z'${1}'[^0-9]*$/q' | tac | tail -n+2 | sed -ne '/ Z/,$ p' | sed -e '/ E[0-9]/ q' | sed -ne 's/.* E\([^ ]*\)/G92 E\1/p'`
#fi

#echo ${BG_EX} >> ${SD_PATH}/${FILE_NAME}
#echo 'G91' >> ${SD_PATH}/${FILE_NAME}
#echo 'G1 Z-5' >> ${SD_PATH}/${FILE_NAME}
#echo 'G90' >> ${SD_PATH}/${FILE_NAME}

tac /tmp/plrtmpA.$$ | sed -e '/ Z'${1}'[^0-9]*$/q' | tac | tail -n+2 | sed -ne '/ Z/,$ p' >> ${SD_PATH}/${FILE_NAME}

#tac /tmp/plrtmpA.$$ | sed -e '/ Z'${1}'/q' | tac | tail -n+2 | sed -ne '/ Z/,$ p' >> ${SD_PATH}/${FILE_NAME}

rm /tmp/plrtmpA.$$
